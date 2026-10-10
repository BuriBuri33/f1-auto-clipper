"""SQLite persistence.

The database file is the pipeline's only durable state, and GitHub Actions
runners are ephemeral, so the file is committed back to the repository at the
end of each run (see .github/workflows/monitor.yml). That keeps the recurring
cost at zero and makes every state transition visible in the git history.

Idempotency is enforced in the schema rather than in calling code:

  * UNIQUE(source_id, youtube_video_id) — a video is discovered once
  * UNIQUE(source_video_id, start_ms, end_ms) — a timestamp range is one clip
  * UNIQUE(clip_id, platform) — a clip is published once per platform

So a workflow that is re-run, or that times out halfway and retries, cannot
produce duplicates: the second insert is rejected by the database, not by a
check that might race.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 5


class VideoStatus(str, Enum):
    """Lifecycle of one source video."""

    DISCOVERED = "DISCOVERED"
    DOWNLOADED = "DOWNLOADED"
    TRANSCRIBED = "TRANSCRIBED"
    ANALYZED = "ANALYZED"
    RENDERED = "RENDERED"
    QUEUED = "QUEUED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ClipStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    RENDERED = "RENDERED"
    QUEUED = "QUEUED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class PublicationStatus(str, Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL UNIQUE,
    channel_id     TEXT    NOT NULL UNIQUE,
    enabled        INTEGER NOT NULL DEFAULT 1,
    rights_cleared INTEGER NOT NULL DEFAULT 0,
    rights_note    TEXT    NOT NULL DEFAULT '',
    feed_etag      TEXT,
    feed_modified  TEXT,
    last_checked_at TEXT,
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS source_videos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    youtube_video_id TEXT    NOT NULL,
    title            TEXT    NOT NULL,
    url              TEXT    NOT NULL,
    published_at     TEXT,
    discovered_at    TEXT    NOT NULL,
    status           TEXT    NOT NULL,
    status_detail    TEXT    NOT NULL DEFAULT '',
    retry_count      INTEGER NOT NULL DEFAULT 0,
    processed_at     TEXT,
    UNIQUE (source_id, youtube_video_id)
);

CREATE INDEX IF NOT EXISTS idx_source_videos_status
    ON source_videos (status, published_at DESC);

CREATE TABLE IF NOT EXISTS clips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_video_id INTEGER NOT NULL REFERENCES source_videos(id) ON DELETE CASCADE,
    start_ms        INTEGER NOT NULL,
    end_ms          INTEGER NOT NULL,
    score           REAL,
    reason          TEXT,
    hook            TEXT,
    transcript_text TEXT,
    title           TEXT,
    description     TEXT,
    hashtags        TEXT,
    status          TEXT    NOT NULL,
    rendered_path   TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE (source_video_id, start_ms, end_ms),
    CHECK (end_ms > start_ms)
);

CREATE TABLE IF NOT EXISTS publications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id          INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform         TEXT    NOT NULL,
    platform_post_id TEXT,
    status           TEXT    NOT NULL,
    published_at     TEXT,
    error            TEXT,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (clip_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_publications_published_at
    ON publications (published_at);

CREATE TABLE IF NOT EXISTS api_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    service   TEXT NOT NULL,
    day       TEXT NOT NULL,
    units     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (service, day)
);
"""


# Applied in ascending order on top of the _SCHEMA baseline (which stays at
# v1 forever). A fresh database therefore takes exactly the same path as an
# existing one, which is the only way to be sure the two end up identical.
_MIGRATIONS = {
    2: [
        # Everything the pre-download gate decides on, recorded so a decision
        # is auditable after the fact and a re-run need not re-fetch it.
        "ALTER TABLE source_videos ADD COLUMN duration_seconds INTEGER",
        "ALTER TABLE source_videos ADD COLUMN license TEXT",
        "ALTER TABLE source_videos ADD COLUMN live_broadcast_content TEXT",
        "ALTER TABLE source_videos ADD COLUMN details_checked_at TEXT",
        "ALTER TABLE source_videos ADD COLUMN download_path TEXT",
        "ALTER TABLE source_videos ADD COLUMN download_bytes INTEGER",
        "ALTER TABLE source_videos ADD COLUMN download_format TEXT",
    ],
    3: [
        # The transcript itself lives in a committed file (text, diffable,
        # and it must survive the runner that produced it); these columns
        # record where it is and how it was made.
        "ALTER TABLE source_videos ADD COLUMN transcript_path TEXT",
        "ALTER TABLE source_videos ADD COLUMN transcript_backend TEXT",
        "ALTER TABLE source_videos ADD COLUMN transcript_model TEXT",
        "ALTER TABLE source_videos ADD COLUMN transcript_language TEXT",
        "ALTER TABLE source_videos ADD COLUMN transcript_segments INTEGER",
        "ALTER TABLE source_videos ADD COLUMN transcript_covered_seconds REAL",
        "ALTER TABLE source_videos ADD COLUMN transcript_chunks INTEGER",
        "ALTER TABLE source_videos ADD COLUMN transcribed_at TEXT",
    ],
    4: [
        # Sub-scores are stored alongside final `score` so the weighted
        # formula's inputs are auditable after the fact (spec section 29 —
        # "why was this picked") without re-deriving them, and so the
        # formula's weights can be changed later and re-applied to history.
        "ALTER TABLE clips ADD COLUMN hook_score INTEGER",
        "ALTER TABLE clips ADD COLUMN emotional_score INTEGER",
        "ALTER TABLE clips ADD COLUMN context_score INTEGER",
        "ALTER TABLE clips ADD COLUMN payoff_score INTEGER",
        "ALTER TABLE clips ADD COLUMN shareability_score INTEGER",
        "ALTER TABLE clips ADD COLUMN scoring_method TEXT",   # 'llm' | 'heuristic'
        "ALTER TABLE clips ADD COLUMN model TEXT",
        "ALTER TABLE source_videos ADD COLUMN analyzed_at TEXT",
        "ALTER TABLE source_videos ADD COLUMN candidates_found INTEGER",
    ],
    5: [
        # `clips` had no per-row failure reason until now — source_videos has
        # had one (status_detail) since v1. A clip that fails the quality
        # gate is set REJECTED so the next candidate is tried instead (spec
        # section 12's "backup candidates"), and the reason is what makes
        # that auditable rather than a silent skip.
        "ALTER TABLE clips ADD COLUMN status_detail TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE clips ADD COLUMN rendered_at TEXT",
    ],
}


def utcnow() -> str:
    """UTC timestamp, second precision, ISO 8601. One format everywhere."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating the parent directory if needed."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """The version recorded in the database, or 0 if it has no schema yet."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["value"]) if row else 0


def init_db(conn: sqlite3.Connection) -> None:
    """Create the baseline schema and apply pending migrations.

    Safe and cheap to call on every run — which is why every command does,
    rather than relying on someone having run `init-db` first.
    """
    current = schema_version(conn)
    conn.executescript(_SCHEMA)

    for version in sorted(_MIGRATIONS):
        if version <= current:
            continue
        for statement in _MIGRATIONS[version]:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                # "duplicate column name" means a previous run applied this
                # migration but died before recording the version. Tolerating
                # exactly that keeps the migration idempotent; anything else
                # is a real error and must not be swallowed.
                if "duplicate column name" not in str(exc).lower():
                    raise

    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception."""
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


# --- sources ----------------------------------------------------------------


def sync_sources(conn: sqlite3.Connection, sources: Iterable[Any]) -> Dict[str, int]:
    """Reconcile config/sources.yaml into the sources table.

    The YAML is the source of truth for enabled/rights_cleared, so this
    overwrites those columns. Sources dropped from the YAML are left in the
    table and disabled rather than deleted: their discovered videos and
    published clips must stay queryable for deduplication.

    Returns a mapping of source name -> row id for every source in the config.
    """
    now = utcnow()
    ids: Dict[str, int] = {}
    configured_names: List[str] = []

    with transaction(conn):
        for source in sources:
            configured_names.append(source.name)
            conn.execute(
                "INSERT INTO sources (name, channel_id, enabled, rights_cleared, "
                "                     rights_note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  channel_id = excluded.channel_id, "
                "  enabled = excluded.enabled, "
                "  rights_cleared = excluded.rights_cleared, "
                "  rights_note = excluded.rights_note",
                (
                    source.name,
                    source.channel_id,
                    1 if source.enabled else 0,
                    1 if source.rights_cleared else 0,
                    source.rights_note,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM sources WHERE name = ?", (source.name,)
            ).fetchone()
            ids[source.name] = int(row["id"])

        if configured_names:
            placeholders = ",".join("?" * len(configured_names))
            conn.execute(
                "UPDATE sources SET enabled = 0 WHERE name NOT IN (%s)" % placeholders,
                configured_names,
            )
        else:
            conn.execute("UPDATE sources SET enabled = 0")

    return ids


def mark_source_checked(
    conn: sqlite3.Connection,
    source_id: int,
    etag: Optional[str] = None,
    modified: Optional[str] = None,
) -> None:
    """Stamp the check time and remember the feed's cache validators.

    The validators are what let the next run send a conditional GET and get a
    304 back, so an unchanged feed costs one tiny request instead of a full
    download — the spec's "do not repeatedly download just to detect change".
    """
    with transaction(conn):
        conn.execute(
            "UPDATE sources SET last_checked_at = ?, "
            "  feed_etag = COALESCE(?, feed_etag), "
            "  feed_modified = COALESCE(?, feed_modified) "
            "WHERE id = ?",
            (utcnow(), etag, modified, source_id),
        )


def get_feed_validators(
    conn: sqlite3.Connection, source_id: int
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(etag, last_modified)`` stored for this source's feed."""
    row = conn.execute(
        "SELECT feed_etag, feed_modified FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return None, None
    return row["feed_etag"], row["feed_modified"]


def list_sources(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM sources ORDER BY name").fetchall())


def set_source_enabled(conn: sqlite3.Connection, name: str, enabled: bool) -> bool:
    """Manual override (spec section 29). Returns False if the name is unknown.

    Note this is a database-level override; the next ``monitor`` run
    re-applies config/sources.yaml, so a lasting change belongs in the YAML.
    """
    with transaction(conn):
        cur = conn.execute(
            "UPDATE sources SET enabled = ? WHERE name = ?",
            (1 if enabled else 0, name),
        )
    return cur.rowcount > 0


# --- source videos ----------------------------------------------------------


def record_discovered_video(
    conn: sqlite3.Connection,
    source_id: int,
    youtube_video_id: str,
    title: str,
    url: str,
    published_at: Optional[str],
    status: VideoStatus = VideoStatus.DISCOVERED,
    status_detail: str = "",
) -> Tuple[bool, int]:
    """Insert a newly seen video.

    Returns ``(is_new, row_id)``. A video already recorded is left untouched —
    including its status — so re-running the monitor never resets work in
    progress, and never re-enqueues something already published or skipped.
    """
    with transaction(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO source_videos "
            "(source_id, youtube_video_id, title, url, published_at, "
            " discovered_at, status, status_detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                youtube_video_id,
                title,
                url,
                published_at,
                utcnow(),
                status.value,
                status_detail,
            ),
        )
        is_new = cur.rowcount > 0
        row = conn.execute(
            "SELECT id FROM source_videos WHERE source_id = ? AND youtube_video_id = ?",
            (source_id, youtube_video_id),
        ).fetchone()
    return is_new, int(row["id"])


def get_video(conn: sqlite3.Connection, youtube_video_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_videos WHERE youtube_video_id = ?",
        (youtube_video_id,),
    ).fetchone()


def set_video_status(
    conn: sqlite3.Connection,
    video_row_id: int,
    status: VideoStatus,
    detail: str = "",
    bump_retry: bool = False,
) -> None:
    """Move a video to a new status, stamping processed_at on terminal states."""
    terminal = {VideoStatus.PUBLISHED, VideoStatus.SKIPPED}
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET status = ?, status_detail = ?, "
            "  retry_count = retry_count + ?, "
            "  processed_at = CASE WHEN ? THEN ? ELSE processed_at END "
            "WHERE id = ?",
            (
                status.value,
                detail,
                1 if bump_retry else 0,
                1 if status in terminal else 0,
                utcnow(),
                video_row_id,
            ),
        )


def videos_by_status(
    conn: sqlite3.Connection, status: VideoStatus, limit: int = 50
) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT sv.*, s.name AS source_name, s.rights_cleared "
            "FROM source_videos sv JOIN sources s ON s.id = sv.source_id "
            "WHERE sv.status = ? "
            "ORDER BY (sv.published_at IS NULL), sv.published_at DESC, sv.id DESC "
            "LIMIT ?",
            (status.value, limit),
        ).fetchall()
    )


def status_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM source_videos GROUP BY status"
    ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def last_check_time(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT MAX(last_checked_at) AS t FROM sources").fetchone()
    return row["t"] if row and row["t"] else None


# --- free-tier usage tracking -----------------------------------------------


def record_api_usage(conn: sqlite3.Connection, service: str, units: int) -> int:
    """Add quota units consumed today for a service. Returns the new total.

    Spec section 25: the pipeline tracks its own free-tier consumption so it
    can stop rather than fail, and never has a reason to reach for a paid
    tier. The day is UTC, which is close enough to the YouTube quota's
    midnight-Pacific reset for a budget check and needs no timezone data.
    """
    day = utcnow()[:10]
    with transaction(conn):
        conn.execute(
            "INSERT INTO api_usage (service, day, units) VALUES (?, ?, ?) "
            "ON CONFLICT(service, day) DO UPDATE SET units = units + excluded.units",
            (service, day, units),
        )
        row = conn.execute(
            "SELECT units FROM api_usage WHERE service = ? AND day = ?", (service, day)
        ).fetchone()
    return int(row["units"])


def api_usage_today(conn: sqlite3.Connection, service: str) -> int:
    row = conn.execute(
        "SELECT units FROM api_usage WHERE service = ? AND day = ?",
        (service, utcnow()[:10]),
    ).fetchone()
    return int(row["units"]) if row else 0


# --- phase 2: video details and download bookkeeping ------------------------


def record_video_details(
    conn: sqlite3.Connection,
    video_row_id: int,
    duration_seconds: Optional[int],
    license_name: Optional[str],
    live_broadcast_content: Optional[str],
) -> None:
    """Store what the pre-download gate decided on, so the decision is auditable."""
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET duration_seconds = ?, license = ?, "
            "  live_broadcast_content = ?, details_checked_at = ? WHERE id = ?",
            (duration_seconds, license_name, live_broadcast_content, utcnow(), video_row_id),
        )


def record_download(
    conn: sqlite3.Connection,
    video_row_id: int,
    path: str,
    size_bytes: int,
    format_id: Optional[str] = None,
) -> None:
    """Mark a video downloaded and remember where the file landed."""
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET status = ?, status_detail = '', "
            "  download_path = ?, download_bytes = ?, download_format = ? "
            "WHERE id = ?",
            (
                VideoStatus.DOWNLOADED.value,
                path,
                size_bytes,
                format_id,
                video_row_id,
            ),
        )


def clear_download(conn: sqlite3.Connection, video_row_id: int) -> None:
    """Forget a download path whose file is gone.

    Called when the recorded file no longer exists — on a CI runner that is
    the normal case between workflow runs, and a stale path that looks valid
    is worse than no path at all.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET download_path = NULL, download_bytes = NULL, "
            "  download_format = NULL WHERE id = ?",
            (video_row_id,),
        )


def videos_pending_download(
    conn: sqlite3.Connection, limit: int = 5, max_retries: int = 3
) -> List[sqlite3.Row]:
    """Videos eligible for a download attempt, newest first.

    DISCOVERED videos, plus FAILED ones still under the retry ceiling — so a
    transient failure is picked up by the next run without manual action, and
    a persistently broken video stops consuming runs.
    """
    return list(
        conn.execute(
            "SELECT sv.*, s.name AS source_name, s.rights_cleared, s.rights_note "
            "FROM source_videos sv JOIN sources s ON s.id = sv.source_id "
            "WHERE (sv.status = ? OR (sv.status = ? AND sv.retry_count < ?)) "
            "  AND s.enabled = 1 "
            "ORDER BY (sv.published_at IS NULL), sv.published_at DESC, sv.id DESC "
            "LIMIT ?",
            (
                VideoStatus.DISCOVERED.value,
                VideoStatus.FAILED.value,
                max_retries,
                limit,
            ),
        ).fetchall()
    )


# --- manual override (spec section 29) --------------------------------------


def count_new_since(conn: sqlite3.Connection, hours: int = 24) -> int:
    """Videos discovered in the last N hours — the spec's "New videos" line."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM source_videos "
        "WHERE discovered_at >= datetime('now', ?)",
        ("-%d hours" % hours,),
    ).fetchone()
    return int(row["n"]) if row else 0


def count_in_progress(conn: sqlite3.Connection) -> int:
    """Videos between discovery and a terminal state — the "Processing" line."""
    in_flight = (
        VideoStatus.DOWNLOADED,
        VideoStatus.TRANSCRIBED,
        VideoStatus.ANALYZED,
        VideoStatus.RENDERED,
        VideoStatus.QUEUED,
    )
    placeholders = ",".join("?" * len(in_flight))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM source_videos WHERE status IN (%s)" % placeholders,
        [s.value for s in in_flight],
    ).fetchone()
    return int(row["n"]) if row else 0


def count_clips_by_status(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM clips GROUP BY status"
    ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}


def retry_failed(
    conn: sqlite3.Connection,
    video_id: Optional[str] = None,
    reset_retry_count: bool = False,
) -> List[str]:
    """Return FAILED videos to DISCOVERED so the next pass picks them up.

    Spec section 29 ("retry failed jobs") and section 23 (every step must be
    retryable). With ``video_id`` it targets one video; without, every FAILED
    one.

    ``reset_retry_count`` zeroes the counter, which is the only way to revive
    a video that has already exhausted its retry ceiling. Off by default:
    silently resetting the counter would turn the ceiling into a no-op and let
    a permanently broken video consume a slot on every run forever.
    """
    if video_id:
        rows = conn.execute(
            "SELECT id, youtube_video_id FROM source_videos "
            "WHERE youtube_video_id = ? AND status = ?",
            (video_id, VideoStatus.FAILED.value),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, youtube_video_id FROM source_videos WHERE status = ?",
            (VideoStatus.FAILED.value,),
        ).fetchall()

    revived: List[str] = []
    with transaction(conn):
        for row in rows:
            conn.execute(
                "UPDATE source_videos SET status = ?, status_detail = '', "
                "  retry_count = CASE WHEN ? THEN 0 ELSE retry_count END "
                "WHERE id = ?",
                (VideoStatus.DISCOVERED.value, 1 if reset_retry_count else 0, row["id"]),
            )
            revived.append(row["youtube_video_id"])
    return revived


def unskip(conn: sqlite3.Connection, video_id: str) -> bool:
    """Return one SKIPPED video to DISCOVERED (spec section 29, manual approve).

    Deliberately one video at a time and never in bulk: most SKIPPED videos
    were skipped by the rights or licence gate, and a bulk un-skip is an easy
    way to push unlicensed footage into the pipeline by accident. The gate
    still re-runs, so this cannot override a rights failure — it only gives a
    video another trip through the checks.
    """
    with transaction(conn):
        cur = conn.execute(
            "UPDATE source_videos SET status = ?, status_detail = '' "
            "WHERE youtube_video_id = ? AND status = ?",
            (VideoStatus.DISCOVERED.value, video_id, VideoStatus.SKIPPED.value),
        )
    return cur.rowcount > 0


# --- phase 3: transcripts ---------------------------------------------------


def record_transcript(
    conn: sqlite3.Connection,
    video_row_id: int,
    path: str,
    backend: str,
    model: Optional[str],
    language: Optional[str],
    segment_count: int,
    covered_seconds: float,
    chunk_count: int,
) -> None:
    """Mark a video transcribed and remember where the transcript landed.

    The transcript file, not the media, is what persists between workflow
    runs: media is far too large to commit and is discarded with the runner,
    while a transcript is text that later phases genuinely need.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET status = ?, status_detail = '', "
            "  transcript_path = ?, transcript_backend = ?, transcript_model = ?, "
            "  transcript_language = ?, transcript_segments = ?, "
            "  transcript_covered_seconds = ?, transcript_chunks = ?, "
            "  transcribed_at = ? "
            "WHERE id = ?",
            (
                VideoStatus.TRANSCRIBED.value,
                path,
                backend,
                model,
                language,
                segment_count,
                covered_seconds,
                chunk_count,
                utcnow(),
                video_row_id,
            ),
        )


def videos_pending_transcription(
    conn: sqlite3.Connection, limit: int = 3, max_retries: int = 3
) -> List[sqlite3.Row]:
    """Videos ready to transcribe, newest first.

    DOWNLOADED videos whose transcript is missing. Note the media file is
    *not* required to still exist: on a fresh runner it never does, which is
    why the ingest command downloads and transcribes in one pass.
    """
    return list(
        conn.execute(
            "SELECT sv.*, s.name AS source_name, s.rights_cleared "
            "FROM source_videos sv JOIN sources s ON s.id = sv.source_id "
            "WHERE sv.status = ? AND s.enabled = 1 AND sv.retry_count < ? "
            "ORDER BY (sv.published_at IS NULL), sv.published_at DESC, sv.id DESC "
            "LIMIT ?",
            (VideoStatus.DOWNLOADED.value, max_retries, limit),
        ).fetchall()
    )


def transcripts_safe_to_prune(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Transcripts for videos that have reached a terminal state.

    Transcripts are committed to the repository so they survive between runs,
    which means they accumulate. Once a video is PUBLISHED or SKIPPED its
    transcript has served its purpose and can go, keeping the repository from
    growing without bound.
    """
    return list(
        conn.execute(
            "SELECT id, youtube_video_id, transcript_path FROM source_videos "
            "WHERE transcript_path IS NOT NULL AND status IN (?, ?)",
            (VideoStatus.PUBLISHED.value, VideoStatus.SKIPPED.value),
        ).fetchall()
    )


def clear_transcript(conn: sqlite3.Connection, video_row_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET transcript_path = NULL WHERE id = ?",
            (video_row_id,),
        )


# --- phase 4: clip candidates ------------------------------------------------


def videos_pending_analysis(
    conn: sqlite3.Connection, limit: int = 3, max_retries: int = 3
) -> List[sqlite3.Row]:
    """Videos ready to analyze: TRANSCRIBED, with a transcript on record.

    Deliberately reads only the committed transcript (via transcript_path),
    never the media — the media is long gone by the time this runs, which is
    exactly why analysis does not need to share a workflow job with ingest.
    """
    return list(
        conn.execute(
            "SELECT sv.*, s.name AS source_name "
            "FROM source_videos sv JOIN sources s ON s.id = sv.source_id "
            "WHERE sv.status = ? AND sv.transcript_path IS NOT NULL "
            "  AND s.enabled = 1 AND sv.retry_count < ? "
            "ORDER BY (sv.published_at IS NULL), sv.published_at DESC, sv.id DESC "
            "LIMIT ?",
            (VideoStatus.TRANSCRIBED.value, max_retries, limit),
        ).fetchall()
    )


def record_clip_candidates(
    conn: sqlite3.Connection,
    source_video_id: int,
    candidates: List[Any],
) -> int:
    """Insert scored candidates as CANDIDATE clips. Returns how many were stored.

    Each candidate is a ``ScoredCandidate`` (analysis.types) — imported
    lazily by callers, never by this module, so ``database`` has no
    dependency on ``analysis``. Duplicate (source_video_id, start_ms, end_ms)
    is silently skipped via INSERT OR IGNORE: the UNIQUE constraint is the
    enforcement, re-running analysis on the same video is a no-op, not an
    error.
    """
    stored = 0
    with transaction(conn):
        for scored in candidates:
            c = scored.candidate
            start_ms = int(round(c.start * 1000))
            end_ms = int(round(c.end * 1000))
            cur = conn.execute(
                "INSERT OR IGNORE INTO clips "
                "(source_video_id, start_ms, end_ms, score, reason, hook, "
                " transcript_text, status, hook_score, emotional_score, "
                " context_score, payoff_score, shareability_score, "
                " scoring_method, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_video_id, start_ms, end_ms, scored.final_score,
                    c.reason, c.hook, c.text, ClipStatus.CANDIDATE.value,
                    c.sub_scores.hook, c.sub_scores.emotional,
                    c.sub_scores.context, c.sub_scores.payoff,
                    c.sub_scores.shareability, c.method, c.model, utcnow(),
                ),
            )
            stored += cur.rowcount
    return stored


def mark_video_analyzed(
    conn: sqlite3.Connection, video_row_id: int, candidates_found: int
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET status = ?, status_detail = '', "
            "  analyzed_at = ?, candidates_found = ? WHERE id = ?",
            (VideoStatus.ANALYZED.value, utcnow(), candidates_found, video_row_id),
        )


def clips_for_video(conn: sqlite3.Connection, source_video_id: int) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM clips WHERE source_video_id = ? ORDER BY score DESC",
            (source_video_id,),
        ).fetchall()
    )


def clip_texts_by_status(
    conn: sqlite3.Connection, statuses: Sequence[str]
) -> List[str]:
    """Transcript text of clips in the given statuses — the pool checked for
    cross-video near-duplicates (QUEUED/PUBLISHED, typically)."""
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        "SELECT transcript_text FROM clips WHERE status IN (%s) "
        "AND transcript_text IS NOT NULL" % placeholders,
        list(statuses),
    ).fetchall()
    return [r["transcript_text"] for r in rows]


def best_candidates_overall(
    conn: sqlite3.Connection, limit: int = 10
) -> List[sqlite3.Row]:
    """Top-scoring CANDIDATE clips across every video, highest first.

    Infrastructure for Phase 9's daily queue (spec section 12), which picks
    the single best candidate across ALL videos and keeps the rest as
    backups. Not used to enforce "1 clip/day" here — that enforcement is
    explicitly Phase 9's job, not Phase 4's.
    """
    return list(
        conn.execute(
            "SELECT c.*, sv.youtube_video_id, sv.title AS video_title "
            "FROM clips c JOIN source_videos sv ON sv.id = c.source_video_id "
            "WHERE c.status = ? ORDER BY c.score DESC LIMIT ?",
            (ClipStatus.CANDIDATE.value, limit),
        ).fetchall()
    )


# --- phase 5: rendering -------------------------------------------------


def videos_pending_render(
    conn: sqlite3.Connection, limit: int = 3, max_retries: int = 3
) -> List[sqlite3.Row]:
    """ANALYZED videos ready to render, newest first.

    ANALYZED implies at least one CANDIDATE clip exists: Phase 4 sets a video
    to SKIPPED, not ANALYZED, when scoring produced nothing to render.
    """
    return list(
        conn.execute(
            "SELECT sv.*, s.name AS source_name "
            "FROM source_videos sv JOIN sources s ON s.id = sv.source_id "
            "WHERE sv.status = ? AND s.enabled = 1 AND sv.retry_count < ? "
            "ORDER BY (sv.published_at IS NULL), sv.published_at DESC, sv.id DESC "
            "LIMIT ?",
            (VideoStatus.ANALYZED.value, max_retries, limit),
        ).fetchall()
    )


def candidate_clips_for_render(
    conn: sqlite3.Connection, source_video_id: int
) -> List[sqlite3.Row]:
    """CANDIDATE clips for one video, best score first.

    The render stage tries these in order: the top-scored one, then each
    backup in turn if an earlier one fails the quality gate (spec section 12
    — the candidates below the one picked are exactly the backups).
    """
    return list(
        conn.execute(
            "SELECT * FROM clips WHERE source_video_id = ? AND status = ? "
            "ORDER BY score DESC",
            (source_video_id, ClipStatus.CANDIDATE.value),
        ).fetchall()
    )


def record_render(
    conn: sqlite3.Connection,
    clip_id: int,
    rendered_path: str,
    title: str,
    description: str,
    hashtags: str,
) -> None:
    """Mark one clip RENDERED and store its publish-ready metadata."""
    with transaction(conn):
        conn.execute(
            "UPDATE clips SET status = ?, status_detail = '', rendered_path = ?, "
            "  title = ?, description = ?, hashtags = ?, rendered_at = ? "
            "WHERE id = ?",
            (
                ClipStatus.RENDERED.value, rendered_path, title, description,
                hashtags, utcnow(), clip_id,
            ),
        )


def reject_clip(conn: sqlite3.Connection, clip_id: int, reason: str) -> None:
    """A candidate failed to render or failed the quality gate.

    REJECTED rather than CANDIDATE again, so the next render attempt for this
    video tries the next backup instead of repeating the same failure — and
    rather than deleting the row, so the failure stays on the record.
    """
    with transaction(conn):
        conn.execute(
            "UPDATE clips SET status = ?, status_detail = ? WHERE id = ?",
            (ClipStatus.REJECTED.value, reason, clip_id),
        )


def mark_video_rendered(conn: sqlite3.Connection, video_row_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE source_videos SET status = ?, status_detail = '' WHERE id = ?",
            (VideoStatus.RENDERED.value, video_row_id),
        )


def get_clip(conn: sqlite3.Connection, clip_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()


# --- phase 6/7: publishing ----------------------------------------------


def publication_status(
    conn: sqlite3.Connection, clip_id: int, platform: str
) -> Optional[str]:
    """The recorded outcome of publishing one clip to one platform, if any.

    Checked BEFORE attempting an upload, not just relied on as a database
    constraint: a platform already at PUBLISHED must never be re-attempted,
    both to avoid a duplicate post and to avoid spending real quota (YouTube
    charges 1,600 units per upload) on a call whose answer is already known.
    """
    row = conn.execute(
        "SELECT status FROM publications WHERE clip_id = ? AND platform = ?",
        (clip_id, platform),
    ).fetchone()
    return row["status"] if row else None


def record_publication(
    conn: sqlite3.Connection,
    clip_id: int,
    platform: str,
    status: PublicationStatus,
    platform_post_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Record (or update) one platform's publishing outcome for one clip.

    Upserts rather than only inserting: a retry after a FAILED attempt must
    be able to move the same (clip_id, platform) row to PUBLISHED, and
    ``UNIQUE(clip_id, platform)`` is exactly what makes "the second call
    updates instead of duplicating" both correct and race-free.
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO publications "
            "(clip_id, platform, platform_post_id, status, published_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(clip_id, platform) DO UPDATE SET "
            "  platform_post_id = excluded.platform_post_id, "
            "  status = excluded.status, "
            "  published_at = excluded.published_at, "
            "  error = excluded.error, "
            "  retry_count = retry_count + 1",
            (
                clip_id, platform, platform_post_id, status.value,
                utcnow() if status is PublicationStatus.PUBLISHED else None,
                error,
            ),
        )


def mark_clip_published(conn: sqlite3.Connection, clip_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE clips SET status = ?, status_detail = '' WHERE id = ?",
            (ClipStatus.PUBLISHED.value, clip_id),
        )


def count_publications_by_status(
    conn: sqlite3.Connection, platform: str
) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM publications WHERE platform = ? "
        "GROUP BY status",
        (platform,),
    ).fetchall()
    return {row["status"]: int(row["n"]) for row in rows}
