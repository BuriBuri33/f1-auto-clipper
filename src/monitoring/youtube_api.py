"""YouTube upload detection via the Data API v3 — the primary detector.

Chosen over the Atom feed because the feed is unreliable from datacenter IPs
(see youtube_rss). The cost is deliberately tiny:

    playlistItems.list   1 unit per call, up to 50 items
    search.list        100 units per call   <- never used here
    videos.insert     1600 units per call   <- one upload per day

Against the free 10,000 units/day, polling the uploads playlist every two
hours costs 12 units a day and leaves the whole quota for publishing. Using
``search.list`` instead would cost 1,200 units for the same information,
which is why this module resolves uploads through the playlist.

The uploads playlist id is the channel id with ``UC`` replaced by ``UU`` — a
documented property, so no extra channels.list call is needed to find it.

Detection needs only an API KEY, not OAuth: the uploads playlist of a public
channel is public data. The OAuth credentials in .env.example are for
publishing (Phase 6) and are not required here.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .types import WATCH_URL, DetectorError, FeedEntry

API_BASE = "https://www.googleapis.com/youtube/v3"

# Quota units per call, from the official quota cost table. Tracked so the
# pipeline can refuse to act once the free allowance is spent, rather than
# discovering it through a 403.
QUOTA_COST = {"playlistItems.list": 1, "videos.list": 1, "channels.list": 1}
# Verified 2026-10-10 against the official quota table and an independent
# source: the cost is per CALL — the number of `part`s requested and the
# number of ids in a batch do not change it.
DAILY_QUOTA_UNITS = 10_000


class QuotaExceeded(DetectorError):
    """The Data API refused the call because the daily quota is spent."""


def uploads_playlist_id(channel_id: str) -> str:
    """The channel's uploads playlist: UC... -> UU...

    Documented behaviour of the API, and it saves a channels.list call.
    """
    if not channel_id.startswith("UC"):
        raise DetectorError(
            "expected a UC... channel id to derive the uploads playlist, got %r"
            % channel_id
        )
    return "UU" + channel_id[2:]


def _is_quota_error(payload: Dict[str, Any]) -> bool:
    errors = (payload.get("error") or {}).get("errors") or []
    return any(
        e.get("reason") in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
        for e in errors
    )


def fetch_uploads(
    channel_id: str,
    api_key: str,
    max_results: int = 25,
    timeout: int = 20,
    max_retries: int = 4,
    backoff_base: float = 2.0,
    session: Optional[requests.Session] = None,
    sleep=time.sleep,
) -> List[FeedEntry]:
    """Return the channel's most recent uploads, newest first.

    One page only: ``max_results`` is capped at 50 by the API, and discovery
    never needs more than the newest handful. Not paginating is what keeps
    this at a flat 1 unit per source per check.
    """
    if not api_key:
        raise DetectorError("no YouTube API key supplied")
    if not 1 <= max_results <= 50:
        raise DetectorError("max_results must be between 1 and 50, got %d" % max_results)

    http = session or requests
    params = {
        "part": "snippet,contentDetails",
        "playlistId": uploads_playlist_id(channel_id),
        "maxResults": max_results,
        "key": api_key,
    }

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            response = http.get(
                "%s/playlistItems" % API_BASE, params=params, timeout=timeout
            )
        except requests.RequestException as exc:
            last_error = DetectorError("playlistItems.list failed: %s" % exc)
        else:
            if response.status_code == 200:
                return _parse_playlist_items(response.json(), channel_id)

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            if response.status_code == 403 and _is_quota_error(payload):
                # Not retryable today, and retrying burns more quota.
                raise QuotaExceeded(
                    "YouTube Data API quota exhausted for this project "
                    "(10,000 units/day). Discovery will resume after the "
                    "midnight Pacific reset; nothing is lost."
                )
            if response.status_code == 404:
                raise DetectorError(
                    "no uploads playlist for channel %s — the channel id is "
                    "wrong, or the channel has no public uploads" % channel_id
                )
            if response.status_code == 400:
                message = (payload.get("error") or {}).get("message", "bad request")
                raise DetectorError(
                    "playlistItems.list rejected the request: %s "
                    "(usually an invalid or restricted API key)" % message
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = DetectorError(
                    "playlistItems.list returned HTTP %d" % response.status_code
                )
            else:
                raise DetectorError(
                    "playlistItems.list returned HTTP %d" % response.status_code
                )

        if attempt < max_retries - 1:
            delay = backoff_base * (2 ** attempt)
            sleep(delay + random.uniform(0, delay * 0.25))

    raise last_error or DetectorError("playlistItems.list could not be reached")


def _parse_playlist_items(payload: Dict[str, Any], channel_id: str) -> List[FeedEntry]:
    """Normalise a playlistItems.list response into FeedEntry objects."""
    items = payload.get("items")
    if items is None:
        raise DetectorError("playlistItems.list response has no 'items' key")

    entries: List[FeedEntry] = []
    for item in items:
        snippet = item.get("snippet") or {}
        content = item.get("contentDetails") or {}

        video_id = content.get("videoId") or (snippet.get("resourceId") or {}).get(
            "videoId"
        )
        if not video_id:
            continue

        # videoPublishedAt is when the VIDEO went public; snippet.publishedAt is
        # when it was added to the playlist. For a re-added or back-dated video
        # those differ, and the video's own date is the one discovery means.
        published = content.get("videoPublishedAt") or snippet.get("publishedAt")

        title = snippet.get("title") or "(untitled)"
        if title in {"Private video", "Deleted video"}:
            # Still in the playlist, but nothing can be done with it.
            continue

        entries.append(
            FeedEntry(
                video_id=video_id,
                title=title,
                url=WATCH_URL.format(video_id=video_id),
                published_at=published,
                channel_id=snippet.get("channelId") or channel_id,
            )
        )

    entries.sort(key=lambda e: e.published_at or "", reverse=True)
    return entries


# --- video details: the authoritative pre-download gate data ----------------

# videos.list costs 1 unit per CALL, not per part and not per id, and accepts
# up to 50 ids. So licence + duration + live status for 50 videos costs the
# same single unit as asking about one of them.
MAX_IDS_PER_DETAILS_CALL = 50

CREATIVE_COMMONS = "creativeCommon"
STANDARD_LICENSE = "youtube"


@dataclass(frozen=True)
class VideoDetails:
    """What the Data API says about a video, for the pre-download gate.

    This is the authoritative source for ``license``. yt-dlp does not reliably
    expose it — verified 2026-10-10: the field was absent entirely from the
    extractor output for a known CC-BY video — so the licence decision must
    never be based on downloader metadata.
    """

    video_id: str
    title: str
    license: str
    duration_seconds: Optional[int]
    live_broadcast_content: str
    privacy_status: Optional[str] = None

    @property
    def is_creative_commons(self) -> bool:
        return self.license == CREATIVE_COMMONS

    @property
    def is_live_or_upcoming(self) -> bool:
        return self.live_broadcast_content in {"live", "upcoming"}


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_iso8601_duration(value: Optional[str]) -> Optional[int]:
    """Parse the API's ISO 8601 duration (``PT1H2M3S``) into whole seconds.

    Returns None for an absent or unparseable value — notably ``P0D``, which
    is what the API returns for an active livestream. None is propagated
    rather than defaulted to 0, because a 0-second duration would quietly
    pass a minimum-length check that None correctly fails.
    """
    if not value:
        return None
    match = _DURATION_RE.match(value)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict().items() if v is not None}
    total = (
        parts.get("days", 0) * 86400
        + parts.get("hours", 0) * 3600
        + parts.get("minutes", 0) * 60
        + parts.get("seconds", 0)
    )
    # Zero is not a real video length — "P0D" is what the API returns for an
    # active livestream. Returning 0 would quietly satisfy the maximum-length
    # check, so it maps to None ("unknown") and the gate defers instead.
    return total or None


def fetch_video_details(
    video_ids: List[str],
    api_key: str,
    timeout: int = 20,
    max_retries: int = 4,
    backoff_base: float = 2.0,
    session: Optional[requests.Session] = None,
    sleep=time.sleep,
    on_quota_spend=None,
) -> Dict[str, VideoDetails]:
    """Look up licence, duration and live status for up to 50 videos at a time.

    Returns a mapping of video id -> VideoDetails. An id the API does not
    return — deleted, private, or region-blocked — is simply absent from the
    mapping; the caller must treat a missing entry as "cannot verify", never
    as a default.

    ``on_quota_spend(units)`` is called once per API call actually made, so
    the caller can record consumption without this module touching the
    database.
    """
    if not api_key:
        raise DetectorError("no YouTube API key supplied")
    if not video_ids:
        return {}

    http = session or requests
    details: Dict[str, VideoDetails] = {}

    for start in range(0, len(video_ids), MAX_IDS_PER_DETAILS_CALL):
        batch = video_ids[start : start + MAX_IDS_PER_DETAILS_CALL]
        params = {
            "part": "snippet,contentDetails,status",
            "id": ",".join(batch),
            "maxResults": len(batch),
            "key": api_key,
        }
        payload = _get_with_retries(
            http, "%s/videos" % API_BASE, params, timeout, max_retries,
            backoff_base, sleep, "videos.list",
        )
        if on_quota_spend:
            on_quota_spend(QUOTA_COST["videos.list"])

        items = payload.get("items")
        if items is None:
            raise DetectorError("videos.list response has no 'items' key")

        for item in items:
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            status = item.get("status") or {}
            video_id = item.get("id")
            if not video_id:
                continue
            details[video_id] = VideoDetails(
                video_id=video_id,
                title=snippet.get("title") or "(untitled)",
                # Absent means "the API did not say", which must not read as
                # Creative Commons. Default to the restrictive value.
                license=status.get("license") or STANDARD_LICENSE,
                duration_seconds=parse_iso8601_duration(content.get("duration")),
                live_broadcast_content=snippet.get("liveBroadcastContent") or "none",
                privacy_status=status.get("privacyStatus"),
            )

    return details


def _get_with_retries(
    http, url, params, timeout, max_retries, backoff_base, sleep, what
) -> Dict[str, Any]:
    """Shared retry/backoff and error classification for Data API GETs."""
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            response = http.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_error = DetectorError("%s failed: %s" % (what, exc))
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise DetectorError("%s returned non-JSON: %s" % (what, exc))

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            if response.status_code == 403 and _is_quota_error(payload):
                raise QuotaExceeded(
                    "YouTube Data API quota exhausted for this project "
                    "(10,000 units/day). Work stays queued; it resumes after "
                    "the midnight Pacific reset."
                )
            if response.status_code == 400:
                message = (payload.get("error") or {}).get("message", "bad request")
                raise DetectorError(
                    "%s rejected the request: %s (usually an invalid or "
                    "restricted API key)" % (what, message)
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = DetectorError(
                    "%s returned HTTP %d" % (what, response.status_code)
                )
            else:
                raise DetectorError(
                    "%s returned HTTP %d" % (what, response.status_code)
                )

        if attempt < max_retries - 1:
            delay = backoff_base * (2 ** attempt)
            sleep(delay + random.uniform(0, delay * 0.25))

    raise last_error or DetectorError("%s could not be reached" % what)
