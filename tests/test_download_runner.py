"""The download pass, end to end with fakes.

These are the tests that matter most: they check the rights gate holds when
wired to the real orchestration, and that every status transition is the one
that keeps the pass idempotent and retry-safe.
"""

import pytest

from src.database import db
from src.download import runner
from src.download import downloader
from src.utils.config import Config, MonitoringConfig, Source
from src.utils.logging import get_logger
from tests.fakes_ytdlp import REAL_INFO, factory, writes_a_file
from tests.test_youtube_api import FakeResponse, FakeSession


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def ffmpeg_present(monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)


def make_config(**download_overrides):
    download = {
        "require_creative_commons": True,
        "min_source_duration_seconds": 120,
        "preferred_max_height": 1440,
        "allow_progressive_only": False,
        "player_clients": ["default"],
        "max_videos_per_run": 5,
        "max_retries": 3,
    }
    download.update(download_overrides)
    return Config(
        database_path=":memory:",
        monitoring=MonitoringConfig(max_retries=1, backoff_base_seconds=0),
        raw={
            "processing": {"max_source_duration_minutes": 180,
                           "max_download_size_mb": 2000},
            "download": download,
        },
    )


def seed(conn, video_id="VID1", rights=True, status=db.VideoStatus.DISCOVERED,
         title="A video", retry_count=0):
    source = Source("src", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=rights, rights_note="n" if rights else "")
    source_id = db.sync_sources(conn, [source])["src"]
    _, row_id = db.record_discovered_video(
        conn, source_id, video_id, title, "https://www.youtube.com/watch?v=%s" % video_id,
        "2026-10-10T00:00:00+00:00", status=status,
    )
    if retry_count:
        for _ in range(retry_count):
            db.set_video_status(conn, row_id, status, bump_retry=True)
    return row_id


def details_response(video_id="VID1", license="creativeCommon", duration="PT10M",
                     live="none"):
    return FakeResponse(200, {"items": [{
        "id": video_id,
        "snippet": {"title": "t", "liveBroadcastContent": live},
        "contentDetails": {"duration": duration},
        "status": {"license": license, "privacyStatus": "public"},
    }]})


def run(conn, tmp_path, session, ydl=None, config=None, api_key="KEY"):
    return runner.run_download(
        conn,
        config or make_config(),
        dest_dir=tmp_path,
        log=get_logger("test"),
        api_key=api_key,
        session=session,
        ydl_factory=ydl or factory({"default": REAL_INFO}, write_file=writes_a_file()),
    )


# --- the happy path --------------------------------------------------------


def test_a_cc_video_from_a_cleared_source_is_downloaded(conn, tmp_path):
    seed(conn)
    result = run(conn, tmp_path, FakeSession([details_response()]))

    assert len(result.downloaded) == 1
    row = db.get_video(conn, "VID1")
    assert row["status"] == "DOWNLOADED"
    assert row["download_path"] and row["download_bytes"] > 0
    assert row["license"] == "creativeCommon"
    assert row["duration_seconds"] == 600
    # One batched details call.
    assert result.api_units_spent == 1
    assert db.api_usage_today(conn, "youtube_data_api") == 1


def test_details_are_recorded_even_when_the_gate_then_skips(conn, tmp_path):
    """The recorded licence is the audit trail for why it was skipped."""
    seed(conn)
    run(conn, tmp_path, FakeSession([details_response(license="youtube")]))
    row = db.get_video(conn, "VID1")
    assert row["license"] == "youtube"
    assert row["details_checked_at"] is not None
    assert row["status"] == "SKIPPED"


# --- the rights gate, wired up --------------------------------------------


def test_an_uncleared_source_is_never_downloaded(conn, tmp_path):
    seed(conn, rights=False)
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file())
    result = run(conn, tmp_path, FakeSession([details_response()]), ydl=ydl)

    assert len(result.skipped) == 1
    assert db.get_video(conn, "VID1")["status"] == "SKIPPED"
    # The decisive assertion: yt-dlp was never invoked at all.
    assert ydl.calls == []


def test_a_non_cc_video_is_never_downloaded(conn, tmp_path):
    seed(conn)
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file())
    run(conn, tmp_path, FakeSession([details_response(license="youtube")]), ydl=ydl)
    assert ydl.calls == []
    assert "licence" in db.get_video(conn, "VID1")["status_detail"]


def test_without_an_api_key_nothing_is_downloaded(conn, tmp_path):
    """No key means no licence verification, which must mean no download."""
    seed(conn)
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file())
    result = run(conn, tmp_path, FakeSession([]), ydl=ydl, api_key="")

    assert result.downloaded == []
    assert ydl.calls == []
    assert db.get_video(conn, "VID1")["status"] == "DISCOVERED"   # deferred


def test_requiring_cc_can_be_switched_off_for_owned_footage(conn, tmp_path):
    seed(conn)
    config = make_config(require_creative_commons=False)
    result = run(conn, tmp_path, FakeSession([details_response(license="youtube")]),
                 config=config)
    assert len(result.downloaded) == 1


# --- deferral vs skip ------------------------------------------------------


def test_a_live_stream_is_deferred_and_keeps_its_status(conn, tmp_path):
    seed(conn)
    result = run(conn, tmp_path,
                 FakeSession([details_response(live="live", duration="P0D")]))
    assert result.downloaded == []
    assert result.skipped == []
    assert db.get_video(conn, "VID1")["status"] == "DISCOVERED"


def test_a_video_the_api_does_not_return_is_deferred_not_skipped(conn, tmp_path):
    seed(conn)
    result = run(conn, tmp_path, FakeSession([FakeResponse(200, {"items": []})]))
    assert result.downloaded == []
    assert result.skipped == []
    assert db.get_video(conn, "VID1")["status"] == "DISCOVERED"


def test_too_long_is_skipped_terminally(conn, tmp_path):
    seed(conn)
    run(conn, tmp_path, FakeSession([details_response(duration="PT4H")]))
    row = db.get_video(conn, "VID1")
    assert row["status"] == "SKIPPED"
    assert "over the 180m limit" in row["status_detail"]


def test_too_short_is_skipped_terminally(conn, tmp_path):
    seed(conn)
    run(conn, tmp_path, FakeSession([details_response(duration="PT30S")]))
    assert "under the 120s minimum" in db.get_video(conn, "VID1")["status_detail"]


# --- failure, retry and idempotency ---------------------------------------


def test_a_transient_download_failure_becomes_FAILED_with_a_bumped_retry(conn, tmp_path):
    seed(conn)
    ydl = factory({"default": RuntimeError("The page needs to be reloaded.")})
    run(conn, tmp_path, FakeSession([details_response()]), ydl=ydl)
    row = db.get_video(conn, "VID1")
    assert row["status"] == "FAILED"
    assert row["retry_count"] == 1


def test_a_failed_video_is_retried_on_the_next_pass(conn, tmp_path):
    seed(conn, status=db.VideoStatus.FAILED, retry_count=1)
    result = run(conn, tmp_path, FakeSession([details_response()]))
    assert len(result.downloaded) == 1
    assert db.get_video(conn, "VID1")["status"] == "DOWNLOADED"


def test_a_video_past_the_retry_ceiling_is_not_picked_up(conn, tmp_path):
    seed(conn, status=db.VideoStatus.FAILED, retry_count=3)
    result = run(conn, tmp_path, FakeSession([]))
    assert result.considered == 0


def test_a_permanently_unavailable_video_is_skipped_not_retried(conn, tmp_path):
    seed(conn)
    ydl = factory({"default": RuntimeError("Video unavailable")})
    run(conn, tmp_path, FakeSession([details_response()]), ydl=ydl)
    row = db.get_video(conn, "VID1")
    assert row["status"] == "SKIPPED"
    assert row["retry_count"] == 0


def test_an_already_downloaded_video_is_not_downloaded_again(conn, tmp_path):
    seed(conn)
    run(conn, tmp_path, FakeSession([details_response()]))
    second = run(conn, tmp_path, FakeSession([]))
    assert second.considered == 0


def test_a_stale_download_path_is_cleared(conn, tmp_path):
    """On CI the file is gone between runs; a path that looks valid is worse
    than none."""
    row_id = seed(conn)
    db.record_download(conn, row_id, "/nonexistent/gone.mp4", 123)
    db.set_video_status(conn, row_id, db.VideoStatus.DISCOVERED)
    run(conn, tmp_path, FakeSession([details_response()]))
    row = db.get_video(conn, "VID1")
    assert row["status"] == "DOWNLOADED"
    assert row["download_path"] != "/nonexistent/gone.mp4"


# --- quota behaviour ------------------------------------------------------


def test_quota_exhaustion_defers_the_whole_pass_and_downloads_nothing(conn, tmp_path):
    seed(conn)
    payload = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file())
    result = run(conn, tmp_path, FakeSession([FakeResponse(403, payload)]), ydl=ydl)

    assert result.downloaded == []
    assert ydl.calls == []
    assert db.get_video(conn, "VID1")["status"] == "DISCOVERED"


def test_many_candidates_cost_one_quota_unit_between_them(conn, tmp_path):
    source = Source("src", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    source_id = db.sync_sources(conn, [source])["src"]
    ids = ["VID%d" % i for i in range(4)]
    for video_id in ids:
        db.record_discovered_video(
            conn, source_id, video_id, "t", "https://x/watch?v=%s" % video_id,
            "2026-10-10T00:00:00+00:00",
        )
    payload = {"items": [{
        "id": v, "snippet": {"title": "t", "liveBroadcastContent": "none"},
        "contentDetails": {"duration": "PT10M"},
        "status": {"license": "creativeCommon"},
    } for v in ids]}

    def write(dest, info):
        from pathlib import Path
        p = Path(dest) / ("%s.mp4" % info["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 100)
        return p

    ydl = factory({"default": REAL_INFO}, write_file=write)
    result = run(conn, tmp_path, FakeSession([FakeResponse(200, payload)]), ydl=ydl)
    assert result.api_units_spent == 1
    assert result.considered == 4


def test_max_videos_per_run_is_respected(conn, tmp_path):
    source = Source("src", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    source_id = db.sync_sources(conn, [source])["src"]
    for i in range(5):
        db.record_discovered_video(
            conn, source_id, "VID%d" % i, "t", "https://x", "2026-10-10T00:00:00+00:00"
        )
    result = run(conn, tmp_path, FakeSession([FakeResponse(200, {"items": []})]),
                 config=make_config(max_videos_per_run=2))
    assert result.considered == 2


def test_a_disabled_source_contributes_no_candidates(conn, tmp_path):
    row_id = seed(conn)
    db.set_source_enabled(conn, "src", False)
    result = run(conn, tmp_path, FakeSession([]))
    assert result.considered == 0


def test_nothing_pending_makes_no_api_call(conn, tmp_path):
    session = FakeSession([])
    result = run(conn, tmp_path, session)
    assert result.considered == 0
    assert session.calls == []
