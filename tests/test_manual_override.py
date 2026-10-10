"""Manual override (spec section 29) and the status counters (section 31)."""

import pytest

from src.database import db
from src.utils.config import Source


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def seed(conn, video_id="VID1", status=db.VideoStatus.DISCOVERED, retries=0):
    source = Source("src", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    source_id = db.sync_sources(conn, [source])["src"]
    _, row_id = db.record_discovered_video(
        conn, source_id, video_id, "t", "https://x", "2026-10-10T00:00:00+00:00"
    )
    for _ in range(retries):
        db.set_video_status(conn, row_id, db.VideoStatus.FAILED, bump_retry=True)
    db.set_video_status(conn, row_id, status)
    return row_id


# --- retry -----------------------------------------------------------------


def test_retry_requeues_a_failed_video(conn):
    seed(conn, status=db.VideoStatus.FAILED)
    assert db.retry_failed(conn) == ["VID1"]
    assert db.get_video(conn, "VID1")["status"] == "DISCOVERED"


def test_retry_clears_the_stale_failure_reason(conn):
    row_id = seed(conn)
    db.set_video_status(conn, row_id, db.VideoStatus.FAILED, "403 from every client")
    db.retry_failed(conn)
    assert db.get_video(conn, "VID1")["status_detail"] == ""


def test_retry_preserves_the_counter_by_default(conn):
    """Silently zeroing it would make the retry ceiling a no-op."""
    seed(conn, status=db.VideoStatus.FAILED, retries=2)
    db.retry_failed(conn)
    assert db.get_video(conn, "VID1")["retry_count"] == 2


def test_reset_count_is_the_only_way_to_revive_a_video_at_the_ceiling(conn):
    seed(conn, status=db.VideoStatus.FAILED, retries=3)
    assert db.videos_pending_download(conn, max_retries=3) == []

    db.retry_failed(conn, reset_retry_count=True)
    assert db.get_video(conn, "VID1")["retry_count"] == 0
    assert len(db.videos_pending_download(conn, max_retries=3)) == 1


def test_retry_can_target_one_video(conn):
    source = Source("src", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    source_id = db.sync_sources(conn, [source])["src"]
    for v in ("A", "B"):
        _, row_id = db.record_discovered_video(
            conn, source_id, v, "t", "https://x", "2026-10-10T00:00:00+00:00"
        )
        db.set_video_status(conn, row_id, db.VideoStatus.FAILED)

    assert db.retry_failed(conn, video_id="A") == ["A"]
    assert db.get_video(conn, "A")["status"] == "DISCOVERED"
    assert db.get_video(conn, "B")["status"] == "FAILED"


def test_retry_never_touches_a_non_failed_video(conn):
    """Including PUBLISHED and SKIPPED — a blanket retry must not resurrect
    work that was finished or deliberately rejected."""
    for status in (db.VideoStatus.PUBLISHED, db.VideoStatus.SKIPPED,
                   db.VideoStatus.DOWNLOADED):
        c = db.connect(":memory:")
        db.init_db(c)
        seed(c, status=status)
        assert db.retry_failed(c) == []
        assert db.get_video(c, "VID1")["status"] == status.value
        c.close()


def test_retry_on_an_unknown_id_is_a_no_op(conn):
    seed(conn, status=db.VideoStatus.FAILED)
    assert db.retry_failed(conn, video_id="NOSUCH") == []
    assert db.get_video(conn, "VID1")["status"] == "FAILED"


# --- unskip ----------------------------------------------------------------


def test_unskip_returns_a_skipped_video_to_the_queue(conn):
    seed(conn, status=db.VideoStatus.SKIPPED)
    assert db.unskip(conn, "VID1") is True
    assert db.get_video(conn, "VID1")["status"] == "DISCOVERED"


def test_unskip_does_not_bypass_the_gate_it_only_requeues(conn):
    """The video goes back to DISCOVERED, so the rights and licence checks
    run again. An un-skip is not an approval."""
    row_id = seed(conn)
    db.set_video_status(conn, row_id, db.VideoStatus.SKIPPED,
                        "source is not rights-cleared")
    db.unskip(conn, "VID1")
    row = db.get_video(conn, "VID1")
    assert row["status"] == "DISCOVERED"
    assert row["status_detail"] == ""        # reason cleared, gate will re-decide


def test_unskip_refuses_a_video_that_is_not_skipped(conn):
    seed(conn, status=db.VideoStatus.DISCOVERED)
    assert db.unskip(conn, "VID1") is False


def test_unskip_on_an_unknown_id_is_false(conn):
    assert db.unskip(conn, "NOSUCH") is False


# --- status counters (spec section 31) ------------------------------------


def test_new_videos_counts_recent_discoveries(conn):
    seed(conn)
    assert db.count_new_since(conn, 24) == 1


def test_new_videos_excludes_older_discoveries(conn):
    row_id = seed(conn)
    conn.execute(
        "UPDATE source_videos SET discovered_at = datetime('now', '-3 days') "
        "WHERE id = ?", (row_id,)
    )
    conn.commit()
    assert db.count_new_since(conn, 24) == 0


@pytest.mark.parametrize("status,counted", [
    (db.VideoStatus.DISCOVERED, False),
    (db.VideoStatus.DOWNLOADED, True),
    (db.VideoStatus.TRANSCRIBED, True),
    (db.VideoStatus.ANALYZED, True),
    (db.VideoStatus.RENDERED, True),
    (db.VideoStatus.QUEUED, True),
    (db.VideoStatus.PUBLISHED, False),
    (db.VideoStatus.SKIPPED, False),
    (db.VideoStatus.FAILED, False),
])
def test_processing_counts_only_in_flight_work(conn, status, counted):
    seed(conn, status=status)
    assert db.count_in_progress(conn) == (1 if counted else 0)


def test_candidates_counts_clips(conn):
    row_id = seed(conn)
    assert db.count_clips_by_status(conn) == {}
    conn.execute(
        "INSERT INTO clips (source_video_id, start_ms, end_ms, status, created_at) "
        "VALUES (?, 0, 45000, 'CANDIDATE', ?)", (row_id, db.utcnow())
    )
    conn.commit()
    assert db.count_clips_by_status(conn) == {"CANDIDATE": 1}
