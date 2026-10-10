"""YouTube upload detection via the public Atom feed.

The feed at ``/feeds/videos.xml?channel_id=UC...`` needs no credentials and
costs no API quota, which makes it the ideal detector — except that it is not
dependable. It has been returning intermittent 404s since late 2025, and the
failures are reported to concentrate on datacenter IPs, which is precisely
what a GitHub Actions runner is. Verified from this machine on 2026-10-10:
the endpoint returned HTTP 404 for multiple known-good channels while the
channel pages themselves returned 200.

So this is the FALLBACK detector, not the primary one. ``youtube_api`` is
primary. Keeping RSS wired has a real payoff: it is the only detector that
works with no credentials at all, so a fresh clone can be exercised before
anyone has created an API key.

What the feed does *not* give: duration, view count, or whether an entry is a
regular upload, a Short or a finished livestream. Those are resolved later,
from the download metadata, where they are free.
"""

from __future__ import annotations

import random
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import requests

from .types import WATCH_URL, DetectorError, FeedEntry, FeedUnchanged

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# A feed body larger than this is not a YouTube channel feed; refuse it rather
# than hand an unbounded string to the XML parser.
MAX_FEED_BYTES = 4 * 1024 * 1024


# Re-exported so callers can keep using youtube_rss.FeedError / .FeedEntry.
FeedError = DetectorError
FeedUnchanged = FeedUnchanged
FeedEntry = FeedEntry


def _text(node: Optional[ET.Element]) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


def parse_feed(xml_bytes: bytes) -> List[FeedEntry]:
    """Parse a YouTube channel Atom feed into entries, newest first.

    Entries missing a video id are skipped rather than raising: one malformed
    entry must not discard the rest of the feed.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FeedError("feed is not well-formed XML: %s" % exc) from exc

    if not root.tag.endswith("feed"):
        raise FeedError("expected an Atom <feed> root, got <%s>" % root.tag)

    feed_channel = _text(root.find("yt:channelId", _NS)) or None

    entries: List[FeedEntry] = []
    for node in root.findall("atom:entry", _NS):
        video_id = _text(node.find("yt:videoId", _NS))
        if not video_id:
            # Fall back to the "yt:video:ID" form of <id>.
            raw_id = _text(node.find("atom:id", _NS))
            if raw_id.startswith("yt:video:"):
                video_id = raw_id[len("yt:video:") :]
        if not video_id:
            continue

        link = node.find("atom:link[@rel='alternate']", _NS)
        url = (link.get("href") if link is not None else None) or WATCH_URL.format(
            video_id=video_id
        )

        entries.append(
            FeedEntry(
                video_id=video_id,
                title=_text(node.find("atom:title", _NS)) or "(untitled)",
                url=url,
                published_at=_text(node.find("atom:published", _NS)) or None,
                channel_id=_text(node.find("yt:channelId", _NS)) or feed_channel,
            )
        )

    entries.sort(key=lambda e: e.published_at or "", reverse=True)
    return entries


def fetch_feed(
    channel_id: str,
    feed_url_template: str = (
        "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    ),
    timeout: int = 20,
    max_retries: int = 4,
    backoff_base: float = 2.0,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    session: Optional[requests.Session] = None,
    sleep=time.sleep,
) -> Tuple[List[FeedEntry], Dict[str, Optional[str]]]:
    """Fetch and parse one channel feed.

    Sends a conditional GET when validators are supplied and raises
    ``FeedUnchanged`` on a 304. Retries connection errors, 429 and 5xx with
    exponential backoff plus jitter; a 4xx other than 429 is not retried,
    because a bad channel id will fail identically every time.

    Returns ``(entries, validators)`` where validators holds the ETag and
    Last-Modified to store for the next call.
    """
    url = feed_url_template.format(channel_id=channel_id)
    http = session or requests
    headers = {"User-Agent": "f1-auto-clipper/0.1 (+https://github.com/)"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            response = http.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_error = FeedError("GET %s failed: %s" % (url, exc))
        else:
            if response.status_code == 304:
                raise FeedUnchanged(channel_id)
            if response.status_code == 404:
                raise FeedError(
                    "feed 404 for channel_id %s — the id is wrong, or the "
                    "channel has no public uploads" % channel_id
                )
            if response.status_code == 200:
                body = response.content
                if len(body) > MAX_FEED_BYTES:
                    raise FeedError(
                        "feed for %s is %d bytes, over the %d byte limit"
                        % (channel_id, len(body), MAX_FEED_BYTES)
                    )
                validators = {
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
                return parse_feed(body), validators
            if response.status_code == 429 or response.status_code >= 500:
                last_error = FeedError(
                    "feed for %s returned HTTP %d" % (channel_id, response.status_code)
                )
            else:
                raise FeedError(
                    "feed for %s returned HTTP %d"
                    % (channel_id, response.status_code)
                )

        if attempt < max_retries - 1:
            delay = backoff_base * (2 ** attempt)
            sleep(delay + random.uniform(0, delay * 0.25))

    raise last_error or FeedError("feed for %s could not be fetched" % channel_id)
