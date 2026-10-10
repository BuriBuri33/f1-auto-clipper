import pytest
import requests

from src.monitoring import youtube_api
from src.monitoring.types import DetectorError

CHANNEL = "UCtesta0000000000000000A"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_on_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def page(*items):
    return {"items": list(items)}


def item(video_id, title="A title", video_published="2026-10-10T10:00:00Z",
         playlist_published="2026-10-10T10:00:00Z"):
    return {
        "snippet": {
            "title": title,
            "publishedAt": playlist_published,
            "channelId": CHANNEL,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        },
        "contentDetails": {"videoId": video_id, "videoPublishedAt": video_published},
    }


def test_uploads_playlist_id_swaps_the_prefix():
    assert youtube_api.uploads_playlist_id("UCabcdef") == "UUabcdef"


def test_uploads_playlist_id_rejects_a_non_channel_id():
    with pytest.raises(DetectorError, match="UC... channel id"):
        youtube_api.uploads_playlist_id("PLnotachannel")


def test_fetch_uploads_parses_a_page_and_queries_the_uploads_playlist():
    session = FakeSession([FakeResponse(200, page(item("VID1"), item("VID2")))])
    entries = youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)

    assert [e.video_id for e in entries] == ["VID1", "VID2"]
    assert entries[0].url == "https://www.youtube.com/watch?v=VID1"
    params = session.calls[0]["params"]
    assert params["playlistId"] == "UUtesta0000000000000000A"
    assert params["key"] == "KEY"
    # search.list costs 100 units; the playlist endpoint costs 1. Make sure we
    # are actually hitting the cheap one.
    assert session.calls[0]["url"].endswith("/playlistItems")


def test_video_publish_date_is_preferred_over_the_playlist_date():
    """They differ for a re-added video, and the video's own date is the one
    discovery means."""
    session = FakeSession([
        FakeResponse(200, page(item(
            "VID1",
            video_published="2026-01-01T00:00:00Z",
            playlist_published="2026-10-10T00:00:00Z",
        )))
    ])
    entry = youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)[0]
    assert entry.published_at == "2026-01-01T00:00:00Z"


def test_falls_back_to_the_resource_id_when_content_details_is_absent():
    raw = {"snippet": {"title": "t", "publishedAt": "2026-10-10T00:00:00Z",
                       "resourceId": {"videoId": "VIDX"}}}
    session = FakeSession([FakeResponse(200, page(raw))])
    assert youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)[0].video_id == "VIDX"


def test_private_and_deleted_placeholders_are_dropped():
    session = FakeSession([
        FakeResponse(200, page(
            item("VID1", title="Private video"),
            item("VID2", title="Deleted video"),
            item("VID3", title="A real one"),
        ))
    ])
    entries = youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)
    assert [e.video_id for e in entries] == ["VID3"]


def test_entries_are_sorted_newest_first():
    session = FakeSession([
        FakeResponse(200, page(
            item("OLD", video_published="2026-01-01T00:00:00Z"),
            item("NEW", video_published="2026-10-01T00:00:00Z"),
        ))
    ])
    entries = youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)
    assert [e.video_id for e in entries] == ["NEW", "OLD"]


def test_missing_items_key_is_an_error_not_an_empty_result():
    """An empty list is a real answer; a missing key is a broken response."""
    session = FakeSession([FakeResponse(200, {"kind": "youtube#playlistItemListResponse"})])
    with pytest.raises(DetectorError, match="no 'items' key"):
        youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)


def test_an_empty_playlist_is_an_empty_list():
    session = FakeSession([FakeResponse(200, page())])
    assert youtube_api.fetch_uploads(CHANNEL, "KEY", session=session) == []


def test_quota_exceeded_is_its_own_error_and_is_not_retried():
    """Retrying a spent quota just burns more of it."""
    payload = {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "spent"}}
    session = FakeSession([FakeResponse(403, payload)])
    with pytest.raises(youtube_api.QuotaExceeded, match="10,000 units/day"):
        youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)
    assert len(session.calls) == 1


def test_a_403_that_is_not_about_quota_is_a_plain_error():
    payload = {"error": {"errors": [{"reason": "forbidden"}]}}
    session = FakeSession([FakeResponse(403, payload)])
    with pytest.raises(DetectorError) as exc:
        youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)
    assert not isinstance(exc.value, youtube_api.QuotaExceeded)


def test_404_explains_the_likely_cause_and_is_not_retried():
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(DetectorError, match="channel id is"):
        youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)
    assert len(session.calls) == 1


def test_400_points_at_the_api_key():
    payload = {"error": {"message": "API key not valid"}}
    session = FakeSession([FakeResponse(400, payload)])
    with pytest.raises(DetectorError, match="API key"):
        youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)


def test_server_errors_are_retried_then_succeed():
    session = FakeSession([FakeResponse(503), FakeResponse(200, page(item("VID1")))])
    entries = youtube_api.fetch_uploads(
        CHANNEL, "KEY", session=session, sleep=lambda _: None
    )
    assert [e.video_id for e in entries] == ["VID1"]
    assert len(session.calls) == 2


def test_connection_errors_are_retried():
    session = FakeSession([requests.ConnectionError("dns"), FakeResponse(200, page(item("V")))])
    assert youtube_api.fetch_uploads(
        CHANNEL, "KEY", session=session, sleep=lambda _: None
    )[0].video_id == "V"


def test_retries_give_up_with_growing_backoff():
    session = FakeSession([FakeResponse(500)] * 3)
    slept = []
    with pytest.raises(DetectorError, match="HTTP 500"):
        youtube_api.fetch_uploads(
            CHANNEL, "KEY", max_retries=3, backoff_base=1.0,
            session=session, sleep=slept.append,
        )
    assert len(slept) == 2
    assert slept[1] > slept[0]


def test_a_403_with_an_unparseable_body_is_not_misread_as_quota():
    session = FakeSession([FakeResponse(403, raise_on_json=True)])
    with pytest.raises(DetectorError) as exc:
        youtube_api.fetch_uploads(CHANNEL, "KEY", session=session)
    assert not isinstance(exc.value, youtube_api.QuotaExceeded)


def test_missing_api_key_is_refused_before_any_request():
    session = FakeSession([])
    with pytest.raises(DetectorError, match="no YouTube API key"):
        youtube_api.fetch_uploads(CHANNEL, "", session=session)
    assert session.calls == []


@pytest.mark.parametrize("bad", [0, 51])
def test_max_results_is_bounded(bad):
    with pytest.raises(DetectorError, match="between 1 and 50"):
        youtube_api.fetch_uploads(CHANNEL, "KEY", max_results=bad)


def test_the_documented_quota_cost_is_the_cheap_endpoint():
    """A guard on the number the whole free-tier budget depends on."""
    assert youtube_api.QUOTA_COST["playlistItems.list"] == 1
    assert youtube_api.DAILY_QUOTA_UNITS == 10_000
