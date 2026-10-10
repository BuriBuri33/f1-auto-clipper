from datetime import datetime, timedelta, timezone

import pytest

from src.database import db
from src.monitoring import source_monitor, youtube_rss
from src.utils.config import Config, MonitoringConfig, Source
from src.utils.logging import get_logger
from tests.fixtures_feed import build_feed
from tests.test_youtube_rss import FakeResponse, FakeSession

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


FEED = build_feed(
    recent=iso(NOW - timedelta(hours=2)),
    recent2=iso(NOW - timedelta(hours=20)),
    old=iso(NOW - timedelta(days=30)),
)


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def make_config(max_age_hours=72, backend="rss"):
    return Config(
        database_path=":memory:",
        monitoring=MonitoringConfig(
            backend=backend,
            max_age_hours_on_discovery=max_age_hours,
            max_retries=2,
            backoff_base_seconds=0,
        ),
        raw={},
    )


def cleared(name="cleared_source", channel="UCtesta0000000000000000A"):
    return Source(name, channel, enabled=True, rights_cleared=True, rights_note="mine")


def uncleared(name="uncleared_source", channel="UCtestb0000000000000000B"):
    return Source(name, channel, enabled=True, rights_cleared=False)


def run(conn, sources, session, max_age_hours=72, now=NOW, backend="rss", api_key=""):
    """Default to the rss backend with no key, so these tests stay deterministic."""
    return source_monitor.run_monitor(
        conn, make_config(max_age_hours, backend), sources, log=get_logger("test"),
        api_key=api_key, session=session, now=now,
    )


def test_discovers_recent_videos_and_skips_the_back_catalogue(conn):
    session = FakeSession([FakeResponse(200, FEED, {"ETag": 'W/"a"'})])
    result = run(conn, [cleared()], session)

    assert result.new_count == 2                      # the two inside the 72h window
    assert result.sources[0].skipped == 1             # the 30-day-old one
    assert result.sources[0].entries_seen == 3

    assert db.get_video(conn, "AAAAAAAAAA1")["status"] == "DISCOVERED"
    old = db.get_video(conn, "AAAAAAAAAA3")
    assert old["status"] == "SKIPPED"
    assert "discovery window" in old["status_detail"]


def test_the_discovery_window_is_configurable(conn):
    session = FakeSession([FakeResponse(200, FEED, {})])
    result = run(conn, [cleared()], session, max_age_hours=6)
    assert result.new_count == 1                      # only the 2h-old entry


def test_a_window_of_zero_disables_the_age_filter(conn):
    session = FakeSession([FakeResponse(200, FEED, {})])
    assert run(conn, [cleared()], session, max_age_hours=0).new_count == 3


def test_second_pass_discovers_nothing_new(conn):
    """Idempotency: the pass is safe to re-run, including after a failure."""
    session = FakeSession([FakeResponse(200, FEED, {}), FakeResponse(200, FEED, {})])
    assert run(conn, [cleared()], session).new_count == 2
    assert run(conn, [cleared()], session).new_count == 0
    assert db.status_counts(conn) == {"DISCOVERED": 2, "SKIPPED": 1}


def test_rights_gate_records_everything_as_skipped(conn):
    session = FakeSession([FakeResponse(200, FEED, {})])
    result = run(conn, [uncleared()], session)

    assert result.new_count == 0
    assert result.sources[0].skipped == 3
    for video_id in ("AAAAAAAAAA1", "AAAAAAAAAA2", "AAAAAAAAAA3"):
        row = db.get_video(conn, video_id)
        assert row["status"] == "SKIPPED"
        assert row["status_detail"] == "source is not rights-cleared"


def test_uncleared_entries_are_recorded_so_they_are_not_reconsidered(conn):
    """Recording a skip is what stops it being re-evaluated every pass."""
    session = FakeSession([FakeResponse(200, FEED, {}), FakeResponse(200, FEED, {})])
    run(conn, [uncleared()], session)
    second = run(conn, [uncleared()], session)
    assert second.sources[0].skipped == 0


def test_clearing_the_rights_flag_later_does_not_resurrect_skipped_videos(conn):
    """A known limitation, pinned by a test so it is a decision not a surprise.

    Flipping rights_cleared to true affects videos discovered from then on.
    Re-processing the already-skipped ones is a deliberate manual act (Phase 9
    adds `retry`), not something a config edit does silently.
    """
    session = FakeSession([FakeResponse(200, FEED, {}), FakeResponse(200, FEED, {})])
    run(conn, [uncleared("s", "UCtesta0000000000000000A")], session)
    run(conn, [cleared("s", "UCtesta0000000000000000A")], session)
    assert db.get_video(conn, "AAAAAAAAAA1")["status"] == "SKIPPED"


def test_disabled_source_is_not_requested_at_all(conn):
    session = FakeSession([])
    disabled = Source("off", "UCtesta0000000000000000A", enabled=False, rights_cleared=True,
                      rights_note="mine")
    result = run(conn, [disabled], session)
    assert result.sources == []
    assert session.calls == []


def test_304_is_reported_as_unchanged_and_costs_no_work(conn):
    session = FakeSession([FakeResponse(200, FEED, {"ETag": 'W/"a"'}), FakeResponse(304)])
    run(conn, [cleared()], session)
    result = run(conn, [cleared()], session)
    assert result.sources[0].unchanged is True
    assert result.new_count == 0
    assert session.calls[1]["headers"]["If-None-Match"] == 'W/"a"'


def test_one_failing_source_does_not_stop_the_others(conn):
    session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(200, FEED, {})])
    sources = [
        cleared("broken", "UCtestz0000000000000000Z"),
        cleared("working", "UCtesta0000000000000000A"),
    ]
    result = run(conn, sources, session)
    assert result.error_count == 1
    assert result.new_count == 2
    by_name = {r.source: r for r in result.sources}
    assert by_name["broken"].error is not None
    assert by_name["working"].checked is True


def test_a_failed_source_keeps_its_old_validators(conn):
    """A failure must not poison the cache into skipping real changes."""
    session = FakeSession(
        [FakeResponse(200, FEED, {"ETag": 'W/"a"'}), FakeResponse(500), FakeResponse(500)]
    )
    sources = [cleared()]
    run(conn, sources, session)
    source_id = db.list_sources(conn)[0]["id"]
    run(conn, sources, session)
    assert db.get_feed_validators(conn, source_id)[0] == 'W/"a"'


def test_sources_are_synced_before_checking(conn):
    session = FakeSession([FakeResponse(200, FEED, {})])
    run(conn, [cleared()], session)
    row = db.list_sources(conn)[0]
    assert row["name"] == "cleared_source"
    assert row["last_checked_at"] is not None


# --- backend selection and fallback ----------------------------------------


class RoutingSession:
    """One fake session serving both detectors, routed by URL.

    The two detectors call `get` with different kwargs (headers vs params), so
    a single-signature fake cannot stand in for both.
    """

    def __init__(self, api=None, rss=None):
        self._api = list(api or [])
        self._rss = list(rss or [])
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        which = "api" if "googleapis.com" in url else "rss"
        self.calls.append(which)
        queue = self._api if which == "api" else self._rss
        if not queue:
            raise AssertionError("unexpected %s call to %s" % (which, url))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ApiResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def api_page(*video_ids, published=None):
    published = published or iso(NOW - timedelta(hours=1))
    return {
        "items": [
            {
                "snippet": {"title": "API %s" % v, "channelId": "UCtesta0000000000000000A",
                            "publishedAt": published, "resourceId": {"videoId": v}},
                "contentDetails": {"videoId": v, "videoPublishedAt": published},
            }
            for v in video_ids
        ]
    }


def test_auto_uses_the_api_when_a_key_is_present(conn):
    session = RoutingSession(api=[ApiResponse(200, api_page("APIVID1", "APIVID2"))])
    result = run(conn, [cleared()], session, backend="auto", api_key="KEY")

    assert session.calls == ["api"]
    assert result.sources[0].backend == "api"
    assert result.new_count == 2
    assert db.get_video(conn, "APIVID1")["status"] == "DISCOVERED"
    # 1 quota unit per source per check — the number the budget rests on.
    assert db.api_usage_today(conn, "youtube_data_api") == 1


def test_auto_uses_rss_when_no_key_is_present(conn):
    session = RoutingSession(rss=[FakeResponse(200, FEED, {})])
    result = run(conn, [cleared()], session, backend="auto", api_key="")
    assert session.calls == ["rss"]
    assert result.sources[0].backend == "rss"
    assert db.api_usage_today(conn, "youtube_data_api") == 0


def test_auto_falls_back_to_rss_when_the_api_fails(conn):
    """The real-world case: the API key is wrong or revoked mid-flight."""
    session = RoutingSession(
        api=[ApiResponse(400, {"error": {"message": "API key not valid"}})],
        rss=[FakeResponse(200, FEED, {})],
    )
    result = run(conn, [cleared()], session, backend="auto", api_key="BAD")
    assert session.calls == ["api", "rss"]
    assert result.sources[0].fell_back is True
    assert result.sources[0].backend == "rss"
    assert result.new_count == 2


def test_auto_does_not_fall_back_on_a_spent_quota(conn):
    """Falling back here would hide a quota problem the operator must see."""
    session = RoutingSession(
        api=[ApiResponse(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})],
        rss=[FakeResponse(200, FEED, {})],
    )
    result = run(conn, [cleared()], session, backend="auto", api_key="KEY")
    assert session.calls == ["api"]
    assert "quota" in result.sources[0].error.lower()
    assert result.new_count == 0


def test_backend_api_does_not_silently_fall_back(conn):
    """An explicit choice is honoured, so a misconfiguration is visible."""
    session = RoutingSession(
        api=[ApiResponse(400, {"error": {"message": "bad"}})],
        rss=[FakeResponse(200, FEED, {})],
    )
    result = run(conn, [cleared()], session, backend="api", api_key="KEY")
    assert session.calls == ["api"]
    assert result.sources[0].error is not None
    assert result.sources[0].fell_back is False


def test_backend_rss_never_touches_the_api(conn):
    session = RoutingSession(rss=[FakeResponse(200, FEED, {})])
    run(conn, [cleared()], session, backend="rss", api_key="KEY")
    assert session.calls == ["rss"]


def test_the_rights_gate_applies_to_the_api_backend_too(conn):
    """A gate that only guards one code path is not a gate."""
    session = RoutingSession(api=[ApiResponse(200, api_page("APIVID1"))])
    result = run(conn, [uncleared()], session, backend="auto", api_key="KEY")
    assert result.new_count == 0
    assert db.get_video(conn, "APIVID1")["status"] == "SKIPPED"


def test_the_discovery_window_applies_to_the_api_backend_too(conn):
    session = RoutingSession(
        api=[ApiResponse(200, api_page("OLDVID", published=iso(NOW - timedelta(days=30))))]
    )
    result = run(conn, [cleared()], session, backend="auto", api_key="KEY")
    assert result.new_count == 0
    assert "discovery window" in db.get_video(conn, "OLDVID")["status_detail"]


def test_api_discovery_is_idempotent_across_passes(conn):
    session = RoutingSession(api=[
        ApiResponse(200, api_page("APIVID1")),
        ApiResponse(200, api_page("APIVID1")),
    ])
    assert run(conn, [cleared()], session, backend="auto", api_key="KEY").new_count == 1
    assert run(conn, [cleared()], session, backend="auto", api_key="KEY").new_count == 0
    assert db.api_usage_today(conn, "youtube_data_api") == 2
