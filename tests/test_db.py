import sqlite3

import pytest

from src.database import db
from src.utils.config import Source


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def src(name="s1", channel="UCtesta0000000000000000A", enabled=True, rights=True):
    return Source(
        name=name,
        channel_id=channel,
        enabled=enabled,
        rights_cleared=rights,
        rights_note="test" if rights else "",
    )


def test_init_db_is_idempotent(conn):
    db.init_db(conn)
    db.init_db(conn)
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    assert int(row["value"]) == db.SCHEMA_VERSION


def test_sync_sources_upserts_rather_than_duplicating(conn):
    first = db.sync_sources(conn, [src()])
    second = db.sync_sources(conn, [src()])
    assert first == second
    assert len(db.list_sources(conn)) == 1


def test_sync_sources_applies_yaml_changes(conn):
    db.sync_sources(conn, [src(rights=True)])
    db.sync_sources(conn, [src(rights=False)])
    assert db.list_sources(conn)[0]["rights_cleared"] == 0


def test_source_removed_from_yaml_is_disabled_not_deleted(conn):
    """Its video history must survive, or deduplication silently breaks."""
    ids = db.sync_sources(conn, [src("keep"), src("drop", "UCtestb0000000000000000B")])
    db.record_discovered_video(
        conn, ids["drop"], "VID1", "t", "u", "2026-01-01T00:00:00+00:00"
    )
    db.sync_sources(conn, [src("keep")])
    rows = {r["name"]: r for r in db.list_sources(conn)}
    assert set(rows) == {"keep", "drop"}
    assert rows["drop"]["enabled"] == 0
    assert db.get_video(conn, "VID1") is not None


def test_record_discovered_video_is_new_only_once(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    is_new, row_id = db.record_discovered_video(
        conn, sid, "VID1", "Title", "url", "2026-01-01T00:00:00+00:00"
    )
    assert is_new is True
    again, same_row = db.record_discovered_video(
        conn, sid, "VID1", "Title", "url", "2026-01-01T00:00:00+00:00"
    )
    assert again is False
    assert same_row == row_id


def test_rediscovery_does_not_reset_an_in_flight_status(conn):
    """The whole point of the monitor being re-runnable."""
    sid = db.sync_sources(conn, [src()])["s1"]
    _, row_id = db.record_discovered_video(conn, sid, "VID1", "t", "u", None)
    db.set_video_status(conn, row_id, db.VideoStatus.PUBLISHED)
    db.record_discovered_video(conn, sid, "VID1", "t", "u", None)
    assert db.get_video(conn, "VID1")["status"] == db.VideoStatus.PUBLISHED.value


def test_same_video_id_may_exist_under_two_sources(conn):
    """Uniqueness is (source, video), per the spec — a re-upload is its own row."""
    ids = db.sync_sources(conn, [src("a"), src("b", "UCtestb0000000000000000B")])
    assert db.record_discovered_video(conn, ids["a"], "VID1", "t", "u", None)[0] is True
    assert db.record_discovered_video(conn, ids["b"], "VID1", "t", "u", None)[0] is True


def test_status_transition_stamps_processed_at_only_on_terminal_states(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    _, row_id = db.record_discovered_video(conn, sid, "VID1", "t", "u", None)
    db.set_video_status(conn, row_id, db.VideoStatus.DOWNLOADED)
    assert db.get_video(conn, "VID1")["processed_at"] is None
    db.set_video_status(conn, row_id, db.VideoStatus.SKIPPED, detail="no rights")
    row = db.get_video(conn, "VID1")
    assert row["processed_at"] is not None
    assert row["status_detail"] == "no rights"


def test_retry_count_only_moves_when_asked(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    _, row_id = db.record_discovered_video(conn, sid, "VID1", "t", "u", None)
    db.set_video_status(conn, row_id, db.VideoStatus.FAILED, bump_retry=True)
    db.set_video_status(conn, row_id, db.VideoStatus.FAILED, bump_retry=True)
    db.set_video_status(conn, row_id, db.VideoStatus.DISCOVERED)
    assert db.get_video(conn, "VID1")["retry_count"] == 2


def test_clip_timestamp_range_is_unique_per_video(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    _, vid = db.record_discovered_video(conn, sid, "VID1", "t", "u", None)
    conn.execute(
        "INSERT INTO clips (source_video_id, start_ms, end_ms, status, created_at) "
        "VALUES (?, 1000, 45000, 'CANDIDATE', ?)",
        (vid, db.utcnow()),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clips (source_video_id, start_ms, end_ms, status, created_at) "
            "VALUES (?, 1000, 45000, 'CANDIDATE', ?)",
            (vid, db.utcnow()),
        )


def test_a_clip_cannot_end_before_it_starts(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    _, vid = db.record_discovered_video(conn, sid, "VID1", "t", "u", None)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clips (source_video_id, start_ms, end_ms, status, created_at) "
            "VALUES (?, 45000, 1000, 'CANDIDATE', ?)",
            (vid, db.utcnow()),
        )


def test_one_publication_per_clip_per_platform(conn):
    """The constraint that makes publishing retry-safe."""
    sid = db.sync_sources(conn, [src()])["s1"]
    _, vid = db.record_discovered_video(conn, sid, "VID1", "t", "u", None)
    cur = conn.execute(
        "INSERT INTO clips (source_video_id, start_ms, end_ms, status, created_at) "
        "VALUES (?, 0, 30000, 'RENDERED', ?)",
        (vid, db.utcnow()),
    )
    clip_id = cur.lastrowid
    conn.execute(
        "INSERT INTO publications (clip_id, platform, status) VALUES (?, 'youtube', 'PUBLISHED')",
        (clip_id,),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO publications (clip_id, platform, status) "
            "VALUES (?, 'youtube', 'PENDING')",
            (clip_id,),
        )
    # A different platform for the same clip is fine.
    conn.execute(
        "INSERT INTO publications (clip_id, platform, status) VALUES (?, 'tiktok', 'PENDING')",
        (clip_id,),
    )
    conn.commit()


def test_feed_validators_round_trip(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    assert db.get_feed_validators(conn, sid) == (None, None)
    db.mark_source_checked(conn, sid, etag='W/"abc"', modified="Wed, 01 Jan 2026 00:00:00 GMT")
    assert db.get_feed_validators(conn, sid) == ('W/"abc"', "Wed, 01 Jan 2026 00:00:00 GMT")
    # A later check with no new validators must not wipe the stored ones.
    db.mark_source_checked(conn, sid)
    assert db.get_feed_validators(conn, sid)[0] == 'W/"abc"'


def test_status_counts_and_last_check(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    db.record_discovered_video(conn, sid, "V1", "t", "u", None)
    db.record_discovered_video(
        conn, sid, "V2", "t", "u", None, status=db.VideoStatus.SKIPPED
    )
    assert db.status_counts(conn) == {"DISCOVERED": 1, "SKIPPED": 1}
    assert db.last_check_time(conn) is None
    db.mark_source_checked(conn, sid)
    assert db.last_check_time(conn) is not None


def test_set_source_enabled_reports_unknown_names(conn):
    db.sync_sources(conn, [src()])
    assert db.set_source_enabled(conn, "s1", False) is True
    assert db.list_sources(conn)[0]["enabled"] == 0
    assert db.set_source_enabled(conn, "nope", False) is False


def test_videos_by_status_orders_newest_first_with_nulls_last(conn):
    sid = db.sync_sources(conn, [src()])["s1"]
    db.record_discovered_video(conn, sid, "OLD", "t", "u", "2026-01-01T00:00:00+00:00")
    db.record_discovered_video(conn, sid, "NEW", "t", "u", "2026-06-01T00:00:00+00:00")
    db.record_discovered_video(conn, sid, "NONE", "t", "u", None)
    ids = [r["youtube_video_id"] for r in db.videos_by_status(conn, db.VideoStatus.DISCOVERED)]
    assert ids == ["NEW", "OLD", "NONE"]
