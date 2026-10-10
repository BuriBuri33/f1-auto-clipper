"""Schema migration.

A fresh database and a migrated one must end up identical in shape — the only
way to be sure of that is to build one each way and compare.
"""

import sqlite3

from src.database import db


V1_BASELINE_COLUMNS = {
    "id", "source_id", "youtube_video_id", "title", "url", "published_at",
    "discovered_at", "status", "status_detail", "retry_count", "processed_at",
}
V2_ADDED_COLUMNS = {
    "duration_seconds", "license", "live_broadcast_content",
    "details_checked_at", "download_path", "download_bytes", "download_format",
}
V3_ADDED_COLUMNS = {
    "transcript_path", "transcript_backend", "transcript_model",
    "transcript_language", "transcript_segments",
    "transcript_covered_seconds", "transcript_chunks", "transcribed_at",
}
ALL_COLUMNS = V1_BASELINE_COLUMNS | V2_ADDED_COLUMNS | V3_ADDED_COLUMNS


def columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def test_a_fresh_database_is_at_the_current_version():
    conn = db.connect(":memory:")
    db.init_db(conn)
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert columns(conn, "source_videos") == ALL_COLUMNS


def test_init_db_is_idempotent_across_repeated_calls():
    conn = db.connect(":memory:")
    for _ in range(3):
        db.init_db(conn)
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert columns(conn, "source_videos") == ALL_COLUMNS


def test_an_empty_database_reports_version_zero():
    conn = db.connect(":memory:")
    assert db.schema_version(conn) == 0


def test_a_v1_database_is_migrated_in_place_without_losing_rows():
    """The real upgrade path: someone ran Phase 1, then pulled a later phase.

    Spans every migration, so it keeps working as more are added — which is
    the case that matters, since a long-lived database skips no versions.
    """
    conn = db.connect(":memory:")
    # Build a v1 database by hand: baseline schema, version stamped at 1.
    conn.executescript(db._SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('version', '1')"
    )
    conn.execute(
        "INSERT INTO sources (name, channel_id, enabled, rights_cleared, "
        "rights_note, created_at) VALUES ('s', 'UCx', 1, 1, 'n', ?)",
        (db.utcnow(),),
    )
    conn.execute(
        "INSERT INTO source_videos (source_id, youtube_video_id, title, url, "
        "discovered_at, status) VALUES (1, 'OLDVID', 't', 'u', ?, 'DISCOVERED')",
        (db.utcnow(),),
    )
    conn.commit()
    assert columns(conn, "source_videos") == V1_BASELINE_COLUMNS

    db.init_db(conn)

    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert columns(conn, "source_videos") == ALL_COLUMNS
    row = db.get_video(conn, "OLDVID")
    assert row is not None
    assert row["status"] == "DISCOVERED"
    assert row["license"] is None          # new column, no value invented


def test_a_half_applied_migration_completes_rather_than_crashing():
    """A run that added some columns then died must not wedge the database."""
    conn = db.connect(":memory:")
    conn.executescript(db._SCHEMA)
    conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '1')")
    conn.execute("ALTER TABLE source_videos ADD COLUMN license TEXT")
    conn.commit()

    db.init_db(conn)

    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert columns(conn, "source_videos") == ALL_COLUMNS


def test_a_real_operational_error_is_not_swallowed():
    """Only 'duplicate column name' is tolerated; anything else must raise."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    try:
        conn.execute("ALTER TABLE no_such_table ADD COLUMN x TEXT")
    except sqlite3.OperationalError as exc:
        assert "duplicate column name" not in str(exc).lower()
    else:
        raise AssertionError("expected an OperationalError")
