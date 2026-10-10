"""Orchestrates one monitoring pass over every configured source.

A pass is idempotent: running it twice in a row records nothing the second
time. Everything that makes that true lives either in the database schema
(UNIQUE(source_id, youtube_video_id)) or in the status rules below, never in
"have I run yet?" state held in memory.

Two gates decide what a newly seen video becomes:

  DISCOVERED  the source is enabled and rights-cleared — eligible for Phase 2
  SKIPPED     the source has no rights clearance, or the video predates the
              discovery window

A SKIPPED video is still recorded. That is deliberate: the record is what
stops it being reconsidered on every later pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from ..database import db
from ..utils.config import Config, Source
from ..utils.logging import ContextLogger, get_logger
from . import youtube_api, youtube_rss
from .types import DetectorError, FeedEntry, FeedUnchanged


@dataclass
class SourceResult:
    """Outcome of checking one source."""

    source: str
    checked: bool = False
    unchanged: bool = False
    entries_seen: int = 0
    new_videos: List[str] = field(default_factory=list)
    skipped: int = 0
    error: Optional[str] = None
    backend: Optional[str] = None
    fell_back: bool = False


@dataclass
class MonitorResult:
    """Outcome of a whole pass."""

    sources: List[SourceResult] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        return sum(len(r.new_videos) for r in self.sources)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.sources if r.error)

    @property
    def checked_count(self) -> int:
        return sum(1 for r in self.sources if r.checked)


def _skip_reason(
    source: Source, entry: youtube_rss.FeedEntry, max_age_hours: int, now: datetime
) -> Optional[str]:
    """Why this entry must not enter the processing pipeline, or None."""
    if not source.rights_cleared:
        return "source is not rights-cleared"
    age = entry.age_hours(now)
    if age is not None and max_age_hours > 0 and age > max_age_hours:
        return "published %.1fh ago, outside the %dh discovery window" % (
            age,
            max_age_hours,
        )
    return None


def _detect_via_api(source, config, api_key, session, conn):
    """Primary detector. Returns (entries, validators)."""
    entries = youtube_api.fetch_uploads(
        channel_id=source.channel_id,
        api_key=api_key,
        max_results=config.monitoring.api_max_results,
        timeout=config.monitoring.request_timeout_seconds,
        max_retries=config.monitoring.max_retries,
        backoff_base=config.monitoring.backoff_base_seconds,
        session=session,
    )
    # Charged whether or not the call returned rows; a 403 is charged too, so
    # it is recorded on the error path by the caller.
    db.record_api_usage(conn, "youtube_data_api", youtube_api.QUOTA_COST["playlistItems.list"])
    return entries, {}


def _detect_via_rss(source, source_id, config, session, conn):
    """Fallback detector. Returns (entries, validators). May raise FeedUnchanged."""
    etag, modified = db.get_feed_validators(conn, source_id)
    return youtube_rss.fetch_feed(
        channel_id=source.channel_id,
        feed_url_template=config.monitoring.feed_url_template,
        timeout=config.monitoring.request_timeout_seconds,
        max_retries=config.monitoring.max_retries,
        backoff_base=config.monitoring.backoff_base_seconds,
        etag=etag,
        last_modified=modified,
        session=session,
    )


def check_source(
    conn,
    source: Source,
    source_id: int,
    config: Config,
    log: ContextLogger,
    api_key: Optional[str] = None,
    session=None,
    now: Optional[datetime] = None,
) -> SourceResult:
    """Check one source for new uploads and record what it found.

    Backend selection:

      ``api``   Data API only — fail loudly if it cannot be reached
      ``rss``   Atom feed only — no credentials, but unreliable from CI
      ``auto``  Data API when a key is present, Atom feed otherwise, and the
                Atom feed as a fallback when the API call fails

    ``auto`` is the default because the two failure modes are uncorrelated:
    the feed 404s from datacenter IPs, the API fails on a spent quota or a
    bad key, and a pass that tries both is far more likely to see an upload
    on the day it happens.
    """
    result = SourceResult(source=source.name)
    now = now or datetime.now(timezone.utc)
    mon = config.monitoring

    backend = mon.backend
    if backend == "auto":
        backend = "api" if api_key else "rss"

    entries: Optional[List[FeedEntry]] = None
    validators: dict = {}

    if backend == "api":
        try:
            entries, validators = _detect_via_api(source, config, api_key, session, conn)
            result.backend = "api"
        except youtube_api.QuotaExceeded as exc:
            db.record_api_usage(conn, "youtube_data_api", 1)
            result.error = str(exc)
            log.error("data api quota exhausted", fields={"source": source.name})
            return result
        except DetectorError as exc:
            if mon.backend != "auto":
                result.error = str(exc)
                log.error("data api check failed", fields={"source": source.name, "error": exc})
                return result
            log.warning(
                "data api check failed, falling back to the atom feed",
                fields={"source": source.name, "error": exc},
            )
            result.fell_back = True
            backend = "rss"

    if entries is None:
        try:
            entries, validators = _detect_via_rss(source, source_id, config, session, conn)
            result.backend = "rss"
        except FeedUnchanged:
            db.mark_source_checked(conn, source_id)
            result.checked = True
            result.unchanged = True
            result.backend = "rss"
            log.info("feed unchanged since last check", fields={"source": source.name})
            return result
        except DetectorError as exc:
            # One bad source must not abort the pass: the others are independent.
            result.error = str(exc)
            log.error("feed check failed", fields={"source": source.name, "error": exc})
            return result

    result.checked = True
    result.entries_seen = len(entries)

    for entry in entries:
        reason = _skip_reason(source, entry, mon.max_age_hours_on_discovery, now)
        status = db.VideoStatus.SKIPPED if reason else db.VideoStatus.DISCOVERED
        is_new, _row_id = db.record_discovered_video(
            conn,
            source_id=source_id,
            youtube_video_id=entry.video_id,
            title=entry.title,
            url=entry.url,
            published_at=entry.published_at,
            status=status,
            status_detail=reason or "",
        )
        if not is_new:
            continue
        if reason:
            result.skipped += 1
            log.info(
                "recorded and skipped",
                fields={"source": source.name, "reason": reason},
            )
        else:
            result.new_videos.append(entry.video_id)
            log.bind(source_video=entry.video_id).info(
                "new video discovered",
                fields={"source": source.name, "title": entry.title},
            )

    db.mark_source_checked(
        conn,
        source_id,
        etag=validators.get("etag"),
        modified=validators.get("last_modified"),
    )
    return result


def run_monitor(
    conn,
    config: Config,
    sources: List[Source],
    log: Optional[ContextLogger] = None,
    api_key: Optional[str] = None,
    session=None,
    now: Optional[datetime] = None,
) -> MonitorResult:
    """Run one monitoring pass over every enabled source."""
    log = log or get_logger(__name__)
    if api_key is None:
        api_key = os.environ.get("YOUTUBE_API_KEY") or None
    if config.monitoring.backend == "auto" and not api_key:
        log.warning(
            "no YOUTUBE_API_KEY set — falling back to the YouTube Atom feed, "
            "which returns intermittent 404s from datacenter IPs and is not "
            "dependable in CI. See docs/LIMITS.md."
        )
    source_ids = db.sync_sources(conn, sources)
    result = MonitorResult()

    for source in sources:
        if not source.enabled:
            log.debug("source disabled, skipping", fields={"source": source.name})
            continue
        if not source.rights_cleared:
            # Still checked, so the feed is recorded and nothing is silently
            # dropped — but every entry lands as SKIPPED.
            log.warning(
                "source is enabled but not rights-cleared: discovering only, "
                "nothing from it will be downloaded or published",
                fields={"source": source.name},
            )
        result.sources.append(
            check_source(
                conn,
                source=source,
                source_id=source_ids[source.name],
                config=config,
                log=log,
                api_key=api_key,
                session=session,
                now=now,
            )
        )

    log.info(
        "monitoring pass complete",
        fields={
            "sources_checked": result.checked_count,
            "new_videos": result.new_count,
            "errors": result.error_count,
            "api_units_today": db.api_usage_today(conn, "youtube_data_api"),
        },
    )
    return result
