"""YouTube Shorts publishing (spec section 19): OAuth refresh-token flow +
resumable upload.

Raw REST, not the ``google-api-python-client``/``google-auth`` SDKs the
original dependency plan named — same reasoning as Phase 4's Gemini client:
the whole protocol is three well-documented HTTP calls (refresh a token,
open a resumable session, PUT the bytes), and minting the refresh token in
the first place is a one-time MANUAL step (see README) this code never
performs. A heavy SDK buys nothing here that ``requests`` does not already
have.

**The audit gate this cannot route around** (docs/LIMITS.md section 2):
videos uploaded from an un-audited API project are forced private by
YouTube regardless of what ``privacyStatus`` is sent — the upload succeeds,
the video is simply locked. ``publishing.force_private`` therefore defaults
to true and sends ``"private"``; flipping it to ``"public"`` before the
audit passes changes nothing except what the request claims to want.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import requests

from .types import PublishAuthError, PublishError

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# YouTube's resumable-upload quota cost, charged once per successful
# videos.insert session regardless of how many PUT requests the upload
# protocol itself takes to complete it.
UPLOAD_QUOTA_UNITS = 1600


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    url: str
    privacy_status: str


def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str,
    session=None, timeout: int = 20,
) -> str:
    """Exchange a long-lived refresh token for a short-lived access token.

    No quota cost: this is Google's OAuth token endpoint, not a Data API
    call, which is also what makes it a cheap, real ``doctor`` check.
    """
    session = session or requests
    try:
        response = session.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PublishError("could not reach Google's OAuth token endpoint: %s" % exc) from exc

    if response.status_code == 400:
        body = _json_or_empty(response)
        raise PublishAuthError(
            "Google rejected the refresh token request (%s): %s. Check "
            "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / "
            "YOUTUBE_REFRESH_TOKEN — a refresh token is revoked if the "
            "OAuth consent screen's test-user list changes or the token "
            "goes unused for 6 months (Google's expiry policy for apps in "
            "'Testing' publishing status)."
            % (body.get("error", "invalid_request"), body.get("error_description", ""))
        )
    if response.status_code != 200:
        raise PublishError(
            "Google's token endpoint returned HTTP %d: %s"
            % (response.status_code, response.text[:300])
        )

    body = _json_or_empty(response)
    token = body.get("access_token")
    if not token:
        raise PublishError("token endpoint returned no access_token: %s" % str(body)[:300])
    return token


def _json_or_empty(response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}


def _open_resumable_session(
    access_token: str, title: str, description: str, tags: List[str],
    privacy_status: str, category_id: str, file_size: int,
    session, timeout: int,
) -> str:
    """POST the video's metadata, get back the session URL bytes are PUT to."""
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }
    try:
        response = session.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": "Bearer %s" % access_token,
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(file_size),
            },
            json=body, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PublishError("could not open a YouTube upload session: %s" % exc) from exc

    if response.status_code == 401:
        raise PublishAuthError("YouTube rejected the access token opening an upload session")
    if response.status_code >= 400:
        raise PublishError(
            "YouTube refused to open an upload session (HTTP %d): %s"
            % (response.status_code, response.text[:400])
        )

    location = response.headers.get("Location")
    if not location:
        raise PublishError("YouTube accepted the upload request but returned no session URL")
    return location


def _query_resume_offset(
    session_url: str, access_token: str, file_size: int, session, timeout: int,
) -> tuple[int, Optional[dict]]:
    """Ask YouTube how many bytes of an interrupted upload it already has.

    Google's documented resumable-upload recovery step: an empty PUT with
    ``Content-Range: bytes */<total>`` returns 308 and a ``Range`` header
    naming the last byte received, or no ``Range`` header at all if nothing
    has arrived yet. If the upload had actually completed despite the
    earlier failure response, this returns 200/201 WITH the finished
    resource's body — returned here rather than discarded, so the caller
    does not need a second round trip to get it.
    """
    try:
        response = session.put(
            session_url,
            headers={
                "Authorization": "Bearer %s" % access_token,
                "Content-Range": "bytes */%d" % file_size,
                "Content-Length": "0",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PublishError("could not query upload resume offset: %s" % exc) from exc

    if response.status_code in (200, 201):
        return file_size, _json_or_empty(response)
    if response.status_code != 308:
        raise PublishError(
            "unexpected status querying upload offset: HTTP %d" % response.status_code
        )
    range_header = response.headers.get("Range")
    if not range_header:
        return 0, None
    # "bytes=0-12345" -> 12346 bytes received so far.
    try:
        return int(range_header.split("-")[-1]) + 1, None
    except ValueError:
        return 0, None


def _put_bytes(
    session_url: str, access_token: str, file_path: Path, offset: int,
    file_size: int, session, timeout: int,
):
    with open(file_path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    headers = {
        "Authorization": "Bearer %s" % access_token,
        "Content-Length": str(len(data)),
        "Content-Range": "bytes %d-%d/%d" % (offset, file_size - 1, file_size),
    }
    return session.put(session_url, data=data, headers=headers, timeout=timeout)


def _upload_bytes(
    session_url: str, access_token: str, file_path: Path, file_size: int,
    session, timeout: int, max_retries: int, backoff_base: float,
    sleep: Callable[[float], None],
) -> dict:
    """PUT the file to the resumable session, resuming on a retryable failure."""
    offset = 0
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            response = _put_bytes(
                session_url, access_token, file_path, offset, file_size, session, timeout,
            )
        except requests.RequestException as exc:
            last_error = PublishError("upload connection dropped: %s" % exc)
        else:
            if response.status_code in (200, 201):
                return _json_or_empty(response)
            if response.status_code == 401:
                raise PublishAuthError("YouTube rejected the access token mid-upload")
            if response.status_code in (308,) or response.status_code >= 500:
                last_error = PublishError(
                    "YouTube returned HTTP %d uploading bytes" % response.status_code
                )
            else:
                raise PublishError(
                    "YouTube rejected the upload (HTTP %d): %s"
                    % (response.status_code, response.text[:400])
                )

        if attempt < max_retries - 1:
            delay = backoff_base * (2 ** attempt)
            sleep(delay + random.uniform(0, delay * 0.25))
            offset, completed_body = _query_resume_offset(
                session_url, access_token, file_size, session, timeout,
            )
            if completed_body is not None:
                # The earlier failure response was misleading — the bytes
                # actually all arrived, and the status query returned the
                # finished resource directly.
                return completed_body

    raise last_error or PublishError("YouTube upload could not be completed")


def upload(
    file_path: Path,
    title: str,
    description: str,
    hashtags: List[str],
    client_id: str,
    client_secret: str,
    refresh_token: str,
    privacy_status: str = "private",
    category_id: str = "17",  # Sports
    session=None,
    timeout: int = 60,
    max_retries: int = 5,
    backoff_base: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    access_token: Optional[str] = None,
    on_quota_spend: Optional[Callable[[int], None]] = None,
) -> UploadResult:
    """Upload one rendered clip as a YouTube Short.

    ``access_token`` lets a caller that already refreshed one (e.g. to run
    `doctor`'s check first) skip a redundant token request; omitted, one is
    fetched here.
    """
    session = session or requests
    file_path = Path(file_path)
    if not file_path.exists():
        raise PublishError("render output does not exist: %s" % file_path)
    file_size = file_path.stat().st_size

    token = access_token or refresh_access_token(
        client_id, client_secret, refresh_token, session=session, timeout=timeout,
    )

    session_url = _open_resumable_session(
        token, title, description, hashtags, privacy_status, category_id,
        file_size, session, timeout,
    )
    info = _upload_bytes(
        session_url, token, file_path, file_size, session, timeout,
        max_retries, backoff_base, sleep,
    )

    video_id = info.get("id")
    if not video_id:
        raise PublishError("YouTube accepted the upload but returned no video id: %s" % str(info)[:300])

    if on_quota_spend:
        on_quota_spend(UPLOAD_QUOTA_UNITS)

    return UploadResult(
        video_id=video_id, url="https://www.youtube.com/watch?v=%s" % video_id,
        privacy_status=privacy_status,
    )
