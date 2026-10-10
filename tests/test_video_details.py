import pytest

from src.monitoring import youtube_api
from src.monitoring.types import DetectorError
from tests.test_youtube_api import FakeResponse, FakeSession


def vid(video_id, license="creativeCommon", duration="PT10M", live="none",
        privacy="public", title="A title"):
    return {
        "id": video_id,
        "snippet": {"title": title, "liveBroadcastContent": live},
        "contentDetails": {"duration": duration},
        "status": {"license": license, "privacyStatus": privacy},
    }


@pytest.mark.parametrize("value,expected", [
    ("PT10M", 600),
    ("PT1H2M3S", 3723),
    ("PT45S", 45),
    ("PT2H", 7200),
    ("P1DT2H", 93600),
    ("P0D", None),        # what the API returns for an active livestream
    ("", None),
    (None, None),
    ("garbage", None),
    ("PT", None),
])
def test_iso8601_duration_parsing(value, expected):
    assert youtube_api.parse_iso8601_duration(value) == expected


def test_a_zero_duration_is_none_not_zero():
    """0 would pass a maximum-length check that None correctly fails."""
    assert youtube_api.parse_iso8601_duration("P0D") is None


def test_fetch_video_details_maps_by_id():
    session = FakeSession([FakeResponse(200, {"items": [
        vid("V1", license="creativeCommon", duration="PT5M"),
        vid("V2", license="youtube", duration="PT3H1M"),
    ]})])
    details = youtube_api.fetch_video_details(["V1", "V2"], "KEY", session=session)

    assert set(details) == {"V1", "V2"}
    assert details["V1"].is_creative_commons is True
    assert details["V1"].duration_seconds == 300
    assert details["V2"].is_creative_commons is False
    assert details["V2"].duration_seconds == 3 * 3600 + 60


def test_an_absent_licence_defaults_to_restricted_not_permissive():
    session = FakeSession([FakeResponse(200, {"items": [
        {"id": "V1", "snippet": {"title": "t"}, "contentDetails": {"duration": "PT5M"},
         "status": {}},
    ]})])
    details = youtube_api.fetch_video_details(["V1"], "KEY", session=session)
    assert details["V1"].license == youtube_api.STANDARD_LICENSE
    assert details["V1"].is_creative_commons is False


def test_a_video_the_api_omits_is_simply_absent():
    """Deleted / private / region-blocked. The caller must read that as
    'cannot verify', which the gate does."""
    session = FakeSession([FakeResponse(200, {"items": [vid("V1")]})])
    details = youtube_api.fetch_video_details(["V1", "GONE"], "KEY", session=session)
    assert "GONE" not in details


def test_requests_are_batched_at_fifty_ids_per_call():
    ids = ["V%03d" % i for i in range(120)]
    session = FakeSession([
        FakeResponse(200, {"items": [vid(i) for i in ids[0:50]]}),
        FakeResponse(200, {"items": [vid(i) for i in ids[50:100]]}),
        FakeResponse(200, {"items": [vid(i) for i in ids[100:120]]}),
    ])
    spent = []
    details = youtube_api.fetch_video_details(
        ids, "KEY", session=session, on_quota_spend=spent.append
    )
    assert len(details) == 120
    assert len(session.calls) == 3
    # 1 unit per CALL, not per id: 120 videos for 3 units, not 120.
    assert spent == [1, 1, 1]
    assert len(session.calls[0]["params"]["id"].split(",")) == 50


def test_all_three_parts_are_requested_in_one_call():
    """Parts do not add quota cost, so asking for all of them is free."""
    session = FakeSession([FakeResponse(200, {"items": [vid("V1")]})])
    youtube_api.fetch_video_details(["V1"], "KEY", session=session)
    part = session.calls[0]["params"]["part"]
    assert set(part.split(",")) == {"snippet", "contentDetails", "status"}


def test_live_status_is_surfaced():
    session = FakeSession([FakeResponse(200, {"items": [
        vid("V1", live="live", duration="P0D"),
    ]})])
    d = youtube_api.fetch_video_details(["V1"], "KEY", session=session)["V1"]
    assert d.is_live_or_upcoming is True
    assert d.duration_seconds is None


def test_empty_id_list_makes_no_request():
    session = FakeSession([])
    assert youtube_api.fetch_video_details([], "KEY", session=session) == {}
    assert session.calls == []


def test_missing_items_key_is_an_error():
    session = FakeSession([FakeResponse(200, {"kind": "x"})])
    with pytest.raises(DetectorError, match="no 'items' key"):
        youtube_api.fetch_video_details(["V1"], "KEY", session=session)


def test_quota_exhaustion_propagates_and_is_not_retried():
    payload = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    session = FakeSession([FakeResponse(403, payload)])
    with pytest.raises(youtube_api.QuotaExceeded):
        youtube_api.fetch_video_details(["V1"], "KEY", session=session)
    assert len(session.calls) == 1


def test_no_api_key_is_refused_before_any_request():
    session = FakeSession([])
    with pytest.raises(DetectorError, match="no YouTube API key"):
        youtube_api.fetch_video_details(["V1"], "", session=session)


def test_server_errors_are_retried():
    session = FakeSession([FakeResponse(500), FakeResponse(200, {"items": [vid("V1")]})])
    details = youtube_api.fetch_video_details(
        ["V1"], "KEY", session=session, sleep=lambda _: None
    )
    assert "V1" in details
