"""Orchestrates one download pass.

Shape of a pass:

    pick candidates  ->  one batched Data API details call  ->  gate each
                     ->  probe  ->  size gate  ->  download  ->  record

The details call is batched deliberately: `videos.list` costs 1 quota unit per
call regardless of how many ids it carries (up to 50), so asking about every
candidate at once costs the same single unit as asking about one.

Status transitions, and why each is what it is:

    ALLOW  + download ok      -> DOWNLOADED
    SKIP   (gate)             -> SKIPPED   terminal; never retried
    DEFER  (gate)             -> DISCOVERED, left alone for the next pass
    download failed, retryable-> FAILED with retry_count bumped
    video permanently gone    -> SKIPPED
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..database import db
from ..monitoring import youtube_api
from ..monitoring.types import DetectorError
from ..utils.config import Config
from ..utils.logging import ContextLogger, get_logger
from . import downloader
from .gate import GateConfig, Verdict, check_download_size, evaluate


@dataclass
class VideoOutcome:
    video_id: str
    title: str
    status: str
    reason: str = ""
    path: Optional[str] = None
    size_bytes: Optional[int] = None


@dataclass
class DownloadRunResult:
    outcomes: List[VideoOutcome] = field(default_factory=list)
    considered: int = 0
    api_units_spent: int = 0

    @property
    def downloaded(self) -> List[VideoOutcome]:
        return [o for o in self.outcomes if o.status == db.VideoStatus.DOWNLOADED.value]

    @property
    def skipped(self) -> List[VideoOutcome]:
        return [o for o in self.outcomes if o.status == db.VideoStatus.SKIPPED.value]

    @property
    def failed(self) -> List[VideoOutcome]:
        return [o for o in self.outcomes if o.status == db.VideoStatus.FAILED.value]


def gate_config_from(config: Config) -> GateConfig:
    processing = config.section("processing")
    dl = config.section("download")
    return GateConfig(
        require_creative_commons=bool(dl.get("require_creative_commons", True)),
        max_source_duration_minutes=int(
            processing.get("max_source_duration_minutes", 180)
        ),
        min_source_duration_seconds=int(dl.get("min_source_duration_seconds", 120)),
        max_download_size_mb=int(processing.get("max_download_size_mb", 2000)),
    )


def run_download(
    conn,
    config: Config,
    dest_dir: Path,
    log: Optional[ContextLogger] = None,
    api_key: Optional[str] = None,
    session=None,
    ydl_factory=None,
) -> DownloadRunResult:
    """Run one download pass and return what happened to each candidate."""
    log = log or get_logger(__name__)
    if api_key is None:
        api_key = os.environ.get("YOUTUBE_API_KEY") or None

    dl_cfg = config.section("download")
    gate_cfg = gate_config_from(config)
    limit = int(dl_cfg.get("max_videos_per_run", 3))
    max_retries = int(dl_cfg.get("max_retries", 3))
    player_clients = tuple(
        dl_cfg.get("player_clients") or downloader.DEFAULT_PLAYER_CLIENTS
    )
    preferred_max_height = int(dl_cfg.get("preferred_max_height", 1440))
    allow_progressive = bool(dl_cfg.get("allow_progressive_only", False))

    result = DownloadRunResult()
    candidates = db.videos_pending_download(conn, limit=limit, max_retries=max_retries)
    result.considered = len(candidates)
    if not candidates:
        log.info("no videos pending download")
        return result

    # --- one batched details call for every candidate ----------------------
    details: Dict[str, youtube_api.VideoDetails] = {}
    if api_key:
        def spend(units: int) -> None:
            result.api_units_spent += units
            db.record_api_usage(conn, "youtube_data_api", units)

        try:
            details = youtube_api.fetch_video_details(
                [row["youtube_video_id"] for row in candidates],
                api_key=api_key,
                timeout=config.monitoring.request_timeout_seconds,
                max_retries=config.monitoring.max_retries,
                backoff_base=config.monitoring.backoff_base_seconds,
                session=session,
                on_quota_spend=spend,
            )
        except youtube_api.QuotaExceeded as exc:
            # Nothing is lost: candidates keep their status and the next run
            # picks them up. Never fall back to downloading unverified.
            log.error("data api quota exhausted, deferring the whole pass",
                      fields={"error": exc})
            return result
        except DetectorError as exc:
            log.error("could not fetch video details, deferring the whole pass",
                      fields={"error": exc})
            return result
    else:
        log.error(
            "no YOUTUBE_API_KEY set. The licence check needs the Data API, so "
            "nothing can be verified and nothing will be downloaded. Set the "
            "key, or set download.require_creative_commons: false ONLY if "
            "every enabled source is footage you own outright."
        )

    for row in candidates:
        outcome = _process_one(
            conn,
            row,
            details.get(row["youtube_video_id"]),
            gate_cfg,
            dest_dir,
            log,
            player_clients=player_clients,
            preferred_max_height=preferred_max_height,
            allow_progressive=allow_progressive,
            ydl_factory=ydl_factory,
        )
        result.outcomes.append(outcome)

    log.info(
        "download pass complete",
        fields={
            "considered": result.considered,
            "downloaded": len(result.downloaded),
            "skipped": len(result.skipped),
            "failed": len(result.failed),
            "api_units": result.api_units_spent,
        },
    )
    return result


def _process_one(
    conn,
    row,
    details,
    gate_cfg: GateConfig,
    dest_dir: Path,
    log: ContextLogger,
    player_clients,
    preferred_max_height: int,
    allow_progressive: bool,
    ydl_factory,
) -> VideoOutcome:
    video_id = row["youtube_video_id"]
    vlog = log.bind(source_video=video_id, retry_count=row["retry_count"])

    if details is not None:
        db.record_video_details(
            conn,
            row["id"],
            duration_seconds=details.duration_seconds,
            license_name=details.license,
            live_broadcast_content=details.live_broadcast_content,
        )

    # A recorded path whose file is gone is worse than no path: on CI that is
    # the normal state between runs.
    if row["download_path"] and not Path(row["download_path"]).exists():
        db.clear_download(conn, row["id"])

    decision = evaluate(bool(row["rights_cleared"]), details, gate_cfg)

    if decision.verdict is Verdict.SKIP:
        db.set_video_status(conn, row["id"], db.VideoStatus.SKIPPED, decision.reason)
        vlog.info("skipped by the gate", fields={"reason": decision.reason})
        return VideoOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value,
                            decision.reason)

    if decision.verdict is Verdict.DEFER:
        # Status deliberately unchanged, so the next pass reconsiders it.
        vlog.info("deferred by the gate", fields={"reason": decision.reason})
        return VideoOutcome(video_id, row["title"], "DEFERRED", decision.reason)

    # --- probe, then the size gate, then the bytes -------------------------
    try:
        probe_result = downloader.probe(
            row["url"], player_clients=player_clients, ydl_factory=ydl_factory
        )
    except downloader.UnavailableVideo as exc:
        reason = "video is unavailable: %s" % exc
        db.set_video_status(conn, row["id"], db.VideoStatus.SKIPPED, reason)
        vlog.info("skipped, video unavailable", fields={"reason": reason})
        return VideoOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value, reason)
    except downloader.DownloadError as exc:
        return _fail(conn, row, vlog, "probe failed: %s" % exc)

    if probe_result.is_live:
        vlog.info("deferred, stream still live", fields={"live": probe_result.live_status})
        return VideoOutcome(video_id, row["title"], "DEFERRED",
                            "stream is %s" % probe_result.live_status)

    size_decision = check_download_size(probe_result.estimated_bytes, gate_cfg)
    if size_decision.verdict is Verdict.SKIP:
        db.set_video_status(conn, row["id"], db.VideoStatus.SKIPPED, size_decision.reason)
        vlog.info("skipped, too large", fields={"reason": size_decision.reason})
        return VideoOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value,
                            size_decision.reason)

    try:
        download_result = downloader.download(
            row["url"],
            dest_dir=dest_dir,
            preferred_max_height=preferred_max_height,
            max_bytes=gate_cfg.max_download_size_mb * 1024 * 1024,
            player_clients=player_clients,
            allow_progressive_only=allow_progressive,
            ydl_factory=ydl_factory,
        )
    except downloader.UnavailableVideo as exc:
        reason = "video is unavailable: %s" % exc
        db.set_video_status(conn, row["id"], db.VideoStatus.SKIPPED, reason)
        return VideoOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value, reason)
    except downloader.DownloadError as exc:
        return _fail(conn, row, vlog, "download failed: %s" % exc)

    db.record_download(
        conn,
        row["id"],
        path=str(download_result.path),
        size_bytes=download_result.size_bytes,
        format_id=download_result.format_id,
    )
    vlog.info(
        "downloaded",
        fields={
            "mb": round(download_result.size_bytes / (1024 * 1024), 1),
            "format": download_result.format_id,
            "client": download_result.player_client,
        },
    )
    return VideoOutcome(
        video_id, row["title"], db.VideoStatus.DOWNLOADED.value,
        path=str(download_result.path), size_bytes=download_result.size_bytes,
    )


def _fail(conn, row, vlog, reason: str) -> VideoOutcome:
    db.set_video_status(
        conn, row["id"], db.VideoStatus.FAILED, reason, bump_retry=True
    )
    vlog.warning("download attempt failed", fields={"reason": reason})
    return VideoOutcome(
        row["youtube_video_id"], row["title"], db.VideoStatus.FAILED.value, reason
    )
