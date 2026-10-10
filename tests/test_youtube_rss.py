from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.monitoring import youtube_rss
from tests.fixtures_feed import build_feed


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
FEED = build_feed(
    recent=iso(NOW - timedelta(hours=2)),
    recent2=iso(NOW - timedelta(hours=20)),
    old=iso(NOW - timedelta(days=30)),
)


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text


class FakeSession:
    """Records requests and replays a scripted list of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_parse_feed_extracts_entries_newest_first():
    entries = youtube_rss.parse_feed(FEED)
    assert [e.video_id for e in entries] == ["AAAAAAAAAA1", "AAAAAAAAAA2", "AAAAAAAAAA3"]
    first = entries[0]
    assert first.title == 'Radio: "He went off the track!"'
    assert first.url == "https://www.youtube.com/watch?v=AAAAAAAAAA1"
    assert first.channel_id == "UCtesta0000000000000000A"


def test_age_hours_is_computed_from_published():
    entries = youtube_rss.parse_feed(FEED)
    assert entries[0].age_hours(NOW) == pytest.approx(2.0, abs=0.01)
    assert entries[2].age_hours(NOW) == pytest.approx(720.0, abs=0.01)


def test_entry_without_published_has_no_age():
    feed = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry><yt:videoId>ZZZZZZZZZZ1</yt:videoId><title>No date</title></entry>
    </feed>"""
    entry = youtube_rss.parse_feed(feed)[0]
    assert entry.published_at is None
    assert entry.age_hours(NOW) is None
    # The canonical watch URL is synthesised when the feed omits the link.
    assert entry.url.endswith("ZZZZZZZZZZ1")


def test_entry_id_falls_back_to_the_yt_video_prefix_form():
    feed = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>yt:video:FALLBACK123</id><title>t</title></entry>
    </feed>"""
    assert youtube_rss.parse_feed(feed)[0].video_id == "FALLBACK123"


def test_one_malformed_entry_does_not_discard_the_others():
    feed = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry><title>no id at all</title></entry>
      <entry><yt:videoId>GOODGOODGO1</yt:videoId><title>fine</title></entry>
    </feed>"""
    assert [e.video_id for e in youtube_rss.parse_feed(feed)] == ["GOODGOODGO1"]


def test_malformed_xml_raises_feed_error():
    with pytest.raises(youtube_rss.FeedError, match="well-formed"):
        youtube_rss.parse_feed(b"<feed><entry>")


def test_wrong_root_element_raises_feed_error():
    with pytest.raises(youtube_rss.FeedError, match="Atom <feed>"):
        youtube_rss.parse_feed(b'<html xmlns="http://www.w3.org/2005/Atom"></html>')


def test_fetch_feed_sends_conditional_headers_and_returns_validators():
    session = FakeSession([FakeResponse(200, FEED, {"ETag": 'W/"v2"', "Last-Modified": "LM"})])
    entries, validators = youtube_rss.fetch_feed(
        "UCtesta0000000000000000A",
        etag='W/"v1"',
        last_modified="earlier",
        session=session,
    )
    assert len(entries) == 3
    assert validators == {"etag": 'W/"v2"', "last_modified": "LM"}
    sent = session.calls[0]["headers"]
    assert sent["If-None-Match"] == 'W/"v1"'
    assert sent["If-Modified-Since"] == "earlier"
    assert "channel_id=UCtesta0000000000000000A" in session.calls[0]["url"]


def test_304_raises_feed_unchanged():
    session = FakeSession([FakeResponse(304)])
    with pytest.raises(youtube_rss.FeedUnchanged):
        youtube_rss.fetch_feed("UCtesta0000000000000000A", etag="x", session=session)


def test_404_is_not_retried_and_explains_the_likely_cause():
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(youtube_rss.FeedError, match="the id is wrong"):
        youtube_rss.fetch_feed("UCtesta0000000000000000A", session=session)
    assert len(session.calls) == 1


def test_403_is_not_retried():
    """A permanent client error repeated four times is four wasted requests."""
    session = FakeSession([FakeResponse(403)])
    with pytest.raises(youtube_rss.FeedError, match="HTTP 403"):
        youtube_rss.fetch_feed("UCtesta0000000000000000A", session=session)
    assert len(session.calls) == 1


def test_429_is_retried_then_succeeds():
    session = FakeSession([FakeResponse(429), FakeResponse(200, FEED, {})])
    slept = []
    entries, _ = youtube_rss.fetch_feed(
        "UCtesta0000000000000000A", session=session, sleep=slept.append
    )
    assert len(entries) == 3
    assert len(session.calls) == 2
    assert slept and slept[0] > 0


def test_server_error_retries_with_growing_backoff_then_gives_up():
    session = FakeSession([FakeResponse(503)] * 3)
    slept = []
    with pytest.raises(youtube_rss.FeedError, match="HTTP 503"):
        youtube_rss.fetch_feed(
            "UCtesta0000000000000000A",
            max_retries=3,
            backoff_base=1.0,
            session=session,
            sleep=slept.append,
        )
    assert len(session.calls) == 3
    assert len(slept) == 2            # no sleep after the final attempt
    assert slept[1] > slept[0]        # exponential, plus jitter


def test_connection_errors_are_retried():
    session = FakeSession(
        [requests.ConnectionError("dns"), FakeResponse(200, FEED, {})]
    )
    entries, _ = youtube_rss.fetch_feed(
        "UCtesta0000000000000000A", session=session, sleep=lambda _: None
    )
    assert len(entries) == 3


def test_oversized_feed_is_refused_before_parsing():
    big = b"<feed xmlns='http://www.w3.org/2005/Atom'>" + b"x" * youtube_rss.MAX_FEED_BYTES
    session = FakeSession([FakeResponse(200, big, {})])
    with pytest.raises(youtube_rss.FeedError, match="over the"):
        youtube_rss.fetch_feed("UCtesta0000000000000000A", session=session)
