"""Instagram Reels publishing (spec section 20): Meta Graph API.

**No direct file upload.** docs/LIMITS.md section 3: the API fetches the
video itself from a public ``video_url`` you supply — so publishing a clip
means hosting it somewhere public first (see ``publishing.hosting``), handing
Meta that URL, and waiting for their servers to finish fetching and
transcoding it before the post can go live.

Three calls, in order:

1. **Create** a media container (``POST /{ig-user-id}/media``) — returns a
   container id immediately; the fetch happens asynchronously.
2. **Poll** the container's ``status_code`` until it reaches ``FINISHED``
   (or fails with ``ERROR``/``EXPIRED``) — there is no webhook, only polling.
3. **Publish** the finished container (``POST /{ig-user-id}/media_publish``)
   — this is the step that actually makes the Reel appear.

Reels via this API are capped at 90 seconds (comfortably above this
project's 20-60s clip range) and the account must be a Professional
(Business/Creator) account — neither is something code can satisfy; both are
one-time account setup, documented in README.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import requests

from . import hosting
from .types import PublishAuthError, PublishError

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
BASE_URL = "https://graph.facebook.com/%s" % GRAPH_API_VERSION

# Processing is roughly proportional to video length; a 20-60s clip has
# measured (per Meta's own docs, not this project — no real account to test
# against yet, see docs/LIMITS.md section 3) well under this in practice.
# Generous because the failure mode of giving up too early is a clip that
# actually would have finished, re-attempted from scratch next run.
DEFAULT_POLL_TIMEOUT_SECONDS = 600
DEFAULT_POLL_INTERVAL_SECONDS = 10

CAPTION_MAX_CHARS = 2200


class InstagramProcessingFailed(PublishError):
    """The container reached ERROR or EXPIRED rather than FINISHED."""


@dataclass(frozen=True)
class PublishResult:
    media_id: str


def _check_response(response, what: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 401 or (
        isinstance(body.get("error"), dict) and body["error"].get("code") == 190
    ):
        raise PublishAuthError(
            "Instagram/Meta rejected the access token %s. META_ACCESS_TOKEN "
            "expires (long-lived tokens last ~60 days) and must be refreshed "
            "manually — see README." % what
        )
    if response.status_code >= 400:
        message = (body.get("error") or {}).get("message", response.text[:300])
        raise PublishError("Instagram API error %s: %s" % (what, message))
    return body


def build_caption(title: str, description: str, hashtags: List[str],
                  limit: int = CAPTION_MAX_CHARS) -> str:
    hashtag_text = " ".join("#%s" % h for h in hashtags)
    caption = "\n\n".join(p for p in (title, description, hashtag_text) if p)
    if len(caption) <= limit:
        return caption
    return caption[: limit - 1].rstrip() + "…"


def get_account_info(
    ig_account_id: str, access_token: str, session=None, timeout: int = 20,
) -> dict:
    """A cheap, side-effect-free call to prove the token and account id work.

    Used by `doctor` — reading the account's own id/username costs nothing
    and needs no special permission beyond what publishing itself needs.
    """
    session = session or requests
    try:
        response = session.get(
            "%s/%s" % (BASE_URL, ig_account_id),
            params={"fields": "id,username", "access_token": access_token},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PublishError("could not reach the Graph API: %s" % exc) from exc
    return _check_response(response, "reading the account")


def create_container(
    ig_user_id: str, video_url: str, caption: str, access_token: str,
    session=None, timeout: int = 30,
) -> str:
    session = session or requests
    try:
        response = session.post(
            "%s/%s/media" % (BASE_URL, ig_user_id),
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": access_token,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PublishError("could not reach the Graph API creating a container: %s" % exc) from exc

    body = _check_response(response, "creating a media container")
    container_id = body.get("id")
    if not container_id:
        raise PublishError("Instagram accepted the container request but returned no id")
    return container_id


def poll_until_ready(
    container_id: str, access_token: str, session=None, timeout: int = 30,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block (via ``sleep``) until the container is ready to publish.

    Raises ``InstagramProcessingFailed`` on ERROR/EXPIRED, ``PublishError``
    if neither outcome is reached within ``poll_timeout``.
    """
    session = session or requests
    elapsed = 0.0
    while True:
        try:
            response = session.get(
                "%s/%s" % (BASE_URL, container_id),
                params={"fields": "status_code,status", "access_token": access_token},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise PublishError("could not poll container status: %s" % exc) from exc

        body = _check_response(response, "polling container status")
        status = body.get("status_code")

        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise InstagramProcessingFailed(
                "container %s reached %s: %s" % (container_id, status, body.get("status", ""))
            )
        # PUBLISHED can appear if a previous attempt already published this
        # exact container — treat it as ready, not a failure.
        if status == "PUBLISHED":
            return

        if elapsed >= poll_timeout:
            raise PublishError(
                "container %s did not finish processing within %ds (last "
                "status: %s)" % (container_id, poll_timeout, status)
            )
        sleep(poll_interval)
        elapsed += poll_interval


def publish_container(
    ig_user_id: str, container_id: str, access_token: str,
    session=None, timeout: int = 30,
) -> str:
    session = session or requests
    try:
        response = session.post(
            "%s/%s/media_publish" % (BASE_URL, ig_user_id),
            data={"creation_id": container_id, "access_token": access_token},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PublishError("could not reach the Graph API publishing: %s" % exc) from exc

    body = _check_response(response, "publishing the container")
    media_id = body.get("id")
    if not media_id:
        raise PublishError("Instagram accepted publish but returned no media id")
    return media_id


def publish(
    file_path: Path,
    title: str,
    description: str,
    hashtags: List[str],
    ig_account_id: str,
    access_token: str,
    github_repo: str,
    github_token: str,
    session=None,
    timeout: int = 30,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    github_release_tag: str = hosting.DEFAULT_RELEASE_TAG,
) -> PublishResult:
    """Host, publish, then un-host. Hosting is cleaned up whether or not the
    publish itself succeeds — a hosted asset has no purpose once this
    function returns, and leaving it up would be silent, growing public
    storage with nothing using it."""
    session = session or requests
    asset = hosting.upload_asset(
        github_repo, file_path, github_token, tag=github_release_tag, session=session,
        timeout=max(timeout, 120),
    )
    try:
        caption = build_caption(title, description, hashtags)
        container_id = create_container(
            ig_account_id, asset.url, caption, access_token, session, timeout,
        )
        poll_until_ready(
            container_id, access_token, session, timeout, poll_interval,
            poll_timeout, sleep,
        )
        media_id = publish_container(ig_account_id, container_id, access_token, session, timeout)
        return PublishResult(media_id=media_id)
    finally:
        try:
            hosting.delete_asset(github_repo, asset.asset_id, github_token, session=session)
        except PublishError as exc:
            # Not re-raised: the publish outcome itself — success or
            # failure — must not be masked by a cleanup problem on the way
            # out. The orphaned asset is harmless (next run reuses the same
            # release) but worth knowing about.
            logger.warning("could not remove hosted asset %s after publishing: %s",
                           asset.url, exc)
