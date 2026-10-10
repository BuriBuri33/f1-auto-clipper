"""Temporary public hosting for Instagram's ``video_url`` requirement.

Spec section 20 / docs/LIMITS.md section 3: the Graph API does not accept a
direct file upload for Reels — it fetches the video itself from a public
HTTPS URL you supply. A GitHub Release asset on this repository satisfies
that at no cost (the reason docs/LIMITS.md section 1 argues for a public
repo in the first place), and — unlike committing the file — is not part of
git history: it is a blob attached to a release, deleted again as soon as
Instagram has ingested it.

Every upload reuses ONE release (``publishing.github_release_tag``,
pre-release, auto-created on first use) rather than creating a new release
per clip, so the repository's Releases page does not accumulate one entry
per published clip — between runs it sits there holding no assets.

Uses ``GITHUB_TOKEN`` — in Actions this is minted automatically with the
``contents: write`` permission the workflow already grants for committing
``data/``, so hosting a clip needs no new secret there. Locally, a personal
access token with the same scope is required to test this module for real.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import requests

from .types import PublishAuthError, PublishError

API_BASE = "https://api.github.com"
UPLOADS_BASE = "https://uploads.github.com"

DEFAULT_RELEASE_TAG = "clip-hosting"


class HostingError(PublishError):
    """The release asset could not be created, uploaded, or removed."""


@dataclass(frozen=True)
class HostedAsset:
    asset_id: int
    url: str


def _headers(token: str) -> dict:
    return {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _check_auth(response) -> None:
    if response.status_code in (401, 403):
        raise PublishAuthError(
            "GitHub rejected the token (HTTP %d) hosting a clip for Instagram. "
            "GITHUB_TOKEN needs the 'contents: write' permission."
            % response.status_code
        )


def _call(session, verb: str, url: str, token: str, timeout: int, **kwargs):
    method = getattr(session, verb)
    try:
        response = method(url, headers=_headers(token), timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise HostingError("could not reach the GitHub API: %s" % exc) from exc
    _check_auth(response)
    return response


def _get_or_create_release(
    repo: str, tag: str, token: str, session, timeout: int,
) -> int:
    get_url = "%s/repos/%s/releases/tags/%s" % (API_BASE, repo, tag)
    response = _call(session, "get", get_url, token, timeout)
    if response.status_code == 200:
        return response.json()["id"]
    if response.status_code != 404:
        raise HostingError(
            "could not look up release %r (HTTP %d): %s"
            % (tag, response.status_code, response.text[:300])
        )

    create_url = "%s/repos/%s/releases" % (API_BASE, repo)
    response = _call(
        session, "post", create_url, token, timeout,
        json={
            "tag_name": tag,
            "name": "Clip hosting (internal)",
            "body": "Holds a rendered clip just long enough for a platform's "
                    "API to fetch it. Created and emptied automatically by "
                    "src/publishing/hosting.py — safe to delete; it is "
                    "recreated on the next publish.",
            "draft": False,
            "prerelease": True,
        },
    )
    if response.status_code != 201:
        raise HostingError(
            "could not create release %r (HTTP %d): %s"
            % (tag, response.status_code, response.text[:300])
        )
    return response.json()["id"]


def upload_asset(
    repo: str, file_path: Path, token: str,
    tag: str = DEFAULT_RELEASE_TAG, session=None, timeout: int = 120,
) -> HostedAsset:
    """Upload a file to the hosting release and return its public URL.

    The returned URL (``browser_download_url``) is reachable with no
    authentication on a public repo — which is exactly what Instagram's
    ``video_url`` fetch needs.
    """
    session = session or requests
    file_path = Path(file_path)
    if not file_path.exists():
        raise HostingError("file to host does not exist: %s" % file_path)

    release_id = _get_or_create_release(repo, tag, token, session, timeout)

    upload_url = (
        "%s/repos/%s/releases/%d/assets" % (UPLOADS_BASE, repo, release_id)
    )
    with open(file_path, "rb") as fh:
        data = fh.read()
    headers = _headers(token)
    headers["Content-Type"] = "video/mp4"
    try:
        response = session.post(
            upload_url, params={"name": file_path.name}, headers=headers,
            data=data, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise HostingError("could not reach the GitHub API: %s" % exc) from exc
    _check_auth(response)
    if response.status_code != 201:
        raise HostingError(
            "could not upload %s as a release asset (HTTP %d): %s"
            % (file_path.name, response.status_code, response.text[:300])
        )
    body = response.json()
    url = body.get("browser_download_url")
    asset_id = body.get("id")
    if not url or not asset_id:
        raise HostingError("GitHub accepted the asset but returned no url/id: %s" % str(body)[:300])
    return HostedAsset(asset_id=asset_id, url=url)


def delete_asset(
    repo: str, asset_id: int, token: str, session=None, timeout: int = 20,
) -> None:
    """Remove a hosted asset. Never raises on a 404 — already gone is fine."""
    session = session or requests
    url = "%s/repos/%s/releases/assets/%d" % (API_BASE, repo, asset_id)
    response = _call(session, "delete", url, token, timeout)
    if response.status_code not in (204, 404):
        raise HostingError(
            "could not delete hosted asset %d (HTTP %d): %s"
            % (asset_id, response.status_code, response.text[:300])
        )
