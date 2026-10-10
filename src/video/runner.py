"""Orchestrates one render pass: a ranked clip candidate -> a publishable MP4.

**Why render re-downloads the source.** Media is never persisted between
workflow runs (`ingest` downloads and transcribes in one pass for exactly
this reason — see ``transcription/runner.py``). By the time a video reaches
ANALYZED the file from its `ingest` run is long gone, so cutting a clip out
of it means fetching it again. A partial, time-ranged fetch was tried first
(yt-dlp's ``download_ranges``) and measured to fail: the googlevideo URLs
handed back for an external-seek download 403'd on every player client that
works for a full download (2026-10-10, see docs/LIMITS.md section 8a). A
full re-download reuses ``download.downloader.download`` — already proven
working in Phase 2 — at the cost of fetching the whole source again for one
20-60s slice. That costs time, not money: Actions minutes are unmetered on
this public repo.

Shape of a pass, per video:

    load transcript
        -> download the source video (once; shared by every candidate tried)
        -> try each CANDIDATE clip, best score first:
               cut + reframe + caption + brand + encode
               -> quality gate (spec section 28)
               -> pass: RENDERED, stop            fail: REJECTED, try the next
        -> every candidate exhausted -> FAILED, retry_count bumped

Status transitions:

    ANALYZED  + a candidate passes the gate  -> RENDERED  (clip AND video)
    ANALYZED  + every candidate rejected     -> FAILED, retry_count bumped
    ANALYZED  + source video now unavailable -> SKIPPED (terminal, as in
                                                 download/runner.py)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..database import db
from ..download import downloader
from ..transcription.types import Transcript
from ..utils.config import REPO_ROOT, Config
from ..utils.logging import ContextLogger, get_logger
from . import branding, captions, ffmpeg, metadata, quality_gate, reframe
from .types import QualityGateFailed, RenderError, RenderResult


@dataclass
class ClipRenderOutcome:
    clip_id: int
    status: str
    reason: str = ""


@dataclass
class VideoRenderOutcome:
    video_id: str
    title: str
    status: str
    reason: str = ""
    clip_id: Optional[int] = None
    attempts: List[ClipRenderOutcome] = field(default_factory=list)


@dataclass
class RenderRunResult:
    outcomes: List[VideoRenderOutcome] = field(default_factory=list)
    environment_error: Optional[str] = None

    @property
    def rendered(self) -> int:
        return sum(1 for o in self.outcomes if o.status == db.VideoStatus.RENDERED.value)

    @property
    def failed(self) -> List[VideoRenderOutcome]:
        return [o for o in self.outcomes if o.status == db.VideoStatus.FAILED.value]


def _resolve_transcript_path(recorded: str) -> Path:
    path = Path(recorded)
    return path if path.is_absolute() else REPO_ROOT / path


def render_clip(
    source_media_path: Path,
    clip_start: float,
    clip_end: float,
    transcript: Transcript,
    source_title: str,
    source_url: str,
    reason: str,
    hook: str,
    out_dir: Path,
    out_name: str,
    video_cfg: dict,
    captions_cfg: dict,
    branding_cfg: dict,
    vocabulary_hint: str = "",
    max_title_chars: int = 95,
    max_hashtags: int = 6,
) -> RenderResult:
    """Render one clip. Raises ``RenderError`` if FFmpeg itself fails.

    Does NOT run the quality gate — that is the caller's job, because only
    the caller knows what to do with a failing result (try the next
    candidate) versus a raised error (same thing, different cause).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ("%s.mp4" % out_name)

    target_width = int(video_cfg.get("width", 1080))
    target_height = int(video_cfg.get("height", 1920))
    target_fps = float(video_cfg.get("fps", 30))
    video_codec = str(video_cfg.get("video_codec", "libx264"))
    audio_codec = str(video_cfg.get("audio_codec", "aac"))
    crf = int(video_cfg.get("crf", 20))
    preset = str(video_cfg.get("preset", "medium"))
    timeout = float(video_cfg.get("timeout_seconds", 300))

    probe_data = ffmpeg.probe(source_media_path)
    video_streams = [s for s in probe_data.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        raise RenderError("source has no video stream: %s" % source_media_path)
    source_width = int(video_streams[0]["width"])
    source_height = int(video_streams[0]["height"])

    decision = reframe.decide(
        str(video_cfg.get("reframe_strategy", "center_crop")),
        source_width, source_height, target_width, target_height,
    )
    graph = reframe.filter_clause(
        decision, source_width, source_height, target_width, target_height,
    )
    stage = "v1"
    graph += "[%s]" % stage

    cue_count = 0
    if captions_cfg.get("enabled", True):
        cues = captions.build_cues(
            transcript.segments, clip_start, clip_end,
            max_chars=int(captions_cfg.get("max_chars_per_cue", 36)),
            max_seconds=float(captions_cfg.get("max_seconds_per_cue", 4.0)),
            pause_threshold=float(captions_cfg.get("pause_threshold_seconds", 0.5)),
        )
        cue_count = len(cues)
        if cues:
            ass_path = out_dir / ("%s.ass" % out_name)
            captions.write_ass(
                cues, ass_path, target_width, target_height,
                font=str(captions_cfg.get("font", captions.DEFAULT_FONT)),
                font_size=int(captions_cfg.get("font_size", captions.DEFAULT_FONT_SIZE)),
                margin_bottom=int(captions_cfg.get("margin_bottom", captions.DEFAULT_MARGIN_BOTTOM)),
                margin_side=int(captions_cfg.get("margin_side", captions.DEFAULT_MARGIN_SIDE)),
            )
            next_stage = "v2"
            graph += ";[%s]ass=%s[%s]" % (
                stage, captions.escape_filter_path(ass_path), next_stage,
            )
            stage = next_stage

    if branding_cfg.get("enabled", True) and str(branding_cfg.get("watermark_text", "")).strip():
        textfile = out_dir / ("%s.watermark.txt" % out_name)
        branding.write_watermark_textfile(str(branding_cfg["watermark_text"]).strip(), textfile)
        next_stage = "v3"
        clause = branding.filter_clause(
            textfile,
            position=str(branding_cfg.get("position", "top_right")),
            caption_margin_bottom=(
                int(captions_cfg.get("margin_bottom", captions.DEFAULT_MARGIN_BOTTOM))
                if captions_cfg.get("enabled", True) else None
            ),
            input_label=stage,
        )
        graph += ";" + clause + "[%s]" % next_stage
        stage = next_stage

    graph += ";[%s]fps=%s[vout]" % (stage, target_fps)

    args = [
        "-ss", "%.3f" % clip_start,
        "-i", str(source_media_path),
        "-t", "%.3f" % (clip_end - clip_start),
        "-filter_complex", graph,
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", video_codec,
        "-crf", str(crf),
        "-preset", preset,
        "-pix_fmt", "yuv420p",
        "-c:a", audio_codec,
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    ffmpeg.run_ffmpeg(args, timeout=timeout)

    out_probe = ffmpeg.probe(out_path)
    duration = float((out_probe.get("format") or {}).get("duration", clip_end - clip_start))

    clip_meta = metadata.build_metadata(
        hook=hook, reason=reason, transcript_text=transcript.text,
        source_title=source_title, source_url=source_url,
        vocabulary_hint=vocabulary_hint,
        max_title_chars=max_title_chars, max_hashtags=max_hashtags,
    )

    return RenderResult(
        path=out_path, duration_seconds=duration, width=target_width,
        height=target_height, fps=target_fps, video_codec=video_codec,
        audio_codec=audio_codec, reframe=decision, caption_cue_count=cue_count,
        metadata=clip_meta,
    )


def render_one_video(
    conn,
    row,
    config: Config,
    work_dir: Path,
    log: ContextLogger,
    ydl_factory=None,
) -> VideoRenderOutcome:
    """Download the source once, then try each candidate clip in score order."""
    video_id = row["youtube_video_id"]
    vlog = log.bind(source_video=video_id)
    video_cfg = config.section("video")
    captions_cfg = config.section("captions")
    branding_cfg = config.section("branding")
    clip_cfg = config.section("clip")
    dl_cfg = config.section("download")
    vocabulary_hint = str(config.section("analysis").get("vocabulary_hint", ""))

    candidates = db.candidate_clips_for_render(conn, row["id"])
    if not candidates:
        return _fail(conn, row, vlog, "no CANDIDATE clips on record for a video marked ANALYZED")

    transcript_path = _resolve_transcript_path(row["transcript_path"])
    if not transcript_path.exists():
        return _fail(conn, row, vlog, "transcript file missing: %s" % transcript_path)
    try:
        transcript = Transcript.read(transcript_path)
    except (ValueError, OSError) as exc:
        return _fail(conn, row, vlog, "could not read transcript: %s" % exc)

    player_clients = tuple(dl_cfg.get("player_clients") or downloader.DEFAULT_PLAYER_CLIENTS)
    preferred_max_height = int(dl_cfg.get("preferred_max_height", 1440))

    try:
        download_result = downloader.download(
            row["url"], dest_dir=work_dir,
            preferred_max_height=preferred_max_height,
            player_clients=player_clients, ydl_factory=ydl_factory,
        )
    except downloader.UnavailableVideo as exc:
        reason = "source video is no longer available: %s" % exc
        db.set_video_status(conn, row["id"], db.VideoStatus.SKIPPED, reason)
        vlog.info("skipped, video unavailable", fields={"reason": reason})
        return VideoRenderOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value, reason)
    except downloader.DownloadError as exc:
        return _fail(conn, row, vlog, "could not re-download source for rendering: %s" % exc)

    outcome = VideoRenderOutcome(video_id, row["title"], "IN_PROGRESS")
    for clip_row in candidates:
        attempt = _attempt_one(
            conn, clip_row, download_result.path, transcript, row, config,
            video_cfg, captions_cfg, branding_cfg, clip_cfg, work_dir,
            vocabulary_hint, vlog,
        )
        outcome.attempts.append(attempt)
        if attempt.status == db.ClipStatus.RENDERED.value:
            db.mark_video_rendered(conn, row["id"])
            outcome.status = db.VideoStatus.RENDERED.value
            outcome.clip_id = attempt.clip_id
            vlog.info("rendered", fields={"clip_id": attempt.clip_id,
                                          "attempts": len(outcome.attempts)})
            return outcome

    reason = "every candidate was rejected (%d tried)" % len(candidates)
    db.set_video_status(conn, row["id"], db.VideoStatus.FAILED, reason, bump_retry=True)
    vlog.warning("render failed, every candidate rejected",
                 fields={"attempts": len(candidates)})
    outcome.status = db.VideoStatus.FAILED.value
    outcome.reason = reason
    return outcome


def _attempt_one(
    conn, clip_row, source_path, transcript, video_row, config,
    video_cfg, captions_cfg, branding_cfg, clip_cfg, work_dir,
    vocabulary_hint, vlog,
) -> ClipRenderOutcome:
    clip_id = int(clip_row["id"])
    clip_start = clip_row["start_ms"] / 1000.0
    clip_end = clip_row["end_ms"] / 1000.0
    clog = vlog.bind(clip_id=clip_id)

    try:
        result = render_clip(
            source_path, clip_start, clip_end, transcript,
            source_title=video_row["title"], source_url=video_row["url"],
            reason=clip_row["reason"] or "", hook=clip_row["hook"] or "",
            out_dir=Path(work_dir) / "rendered",
            out_name="%s_%d" % (video_row["youtube_video_id"], clip_id),
            video_cfg=video_cfg, captions_cfg=captions_cfg, branding_cfg=branding_cfg,
            vocabulary_hint=vocabulary_hint,
            max_title_chars=int(clip_cfg.get("max_title_chars", 95)),
            max_hashtags=int(clip_cfg.get("max_hashtags", 6)),
        )
    except RenderError as exc:
        reason = "FFmpeg render failed: %s" % exc
        db.reject_clip(conn, clip_id, reason)
        clog.warning("candidate rejected, render failed", fields={"reason": reason})
        return ClipRenderOutcome(clip_id, db.ClipStatus.REJECTED.value, reason)

    gate = quality_gate.check(
        result.path, ffmpeg.probe(result.path),
        expected_width=result.width, expected_height=result.height,
        expected_fps=result.fps, expected_video_codec=_codec_short_name(result.video_codec),
        expected_audio_codec=_codec_short_name(result.audio_codec),
        expected_duration=clip_end - clip_start,
    )
    if not gate.passed:
        db.reject_clip(conn, clip_id, gate.reason)
        clog.warning("candidate rejected, quality gate failed", fields={"reason": gate.reason})
        return ClipRenderOutcome(clip_id, db.ClipStatus.REJECTED.value, gate.reason)

    db.record_render(
        conn, clip_id, str(result.path), result.metadata.title,
        result.metadata.description, ",".join(result.metadata.hashtags),
    )
    return ClipRenderOutcome(clip_id, db.ClipStatus.RENDERED.value)


# ffprobe reports codec names ("h264", "aac"), not FFmpeg encoder names
# ("libx264", "aac") — libx264 always produces an h264 stream.
_ENCODER_TO_CODEC_NAME = {"libx264": "h264", "libx265": "hevc", "aac": "aac"}


def _codec_short_name(encoder: str) -> str:
    return _ENCODER_TO_CODEC_NAME.get(encoder, encoder)


def _fail(conn, row, vlog, reason: str) -> VideoRenderOutcome:
    db.set_video_status(conn, row["id"], db.VideoStatus.FAILED, reason, bump_retry=True)
    vlog.warning("render failed", fields={"reason": reason})
    return VideoRenderOutcome(
        row["youtube_video_id"], row["title"], db.VideoStatus.FAILED.value, reason
    )


def run_render(
    conn,
    config: Config,
    work_dir: Path,
    log: Optional[ContextLogger] = None,
    ydl_factory=None,
) -> RenderRunResult:
    """Run one render pass over every video pending render."""
    log = log or get_logger(__name__)
    result = RenderRunResult()

    missing = ffmpeg.missing_required_filters()
    if missing:
        result.environment_error = (
            "FFmpeg is missing required filter(s): %s. A plain `ffmpeg` build "
            "commonly lacks these (no libass/libfreetype) — see "
            "docs/LIMITS.md section 8a. No videos were touched." % ", ".join(missing)
        )
        log.error("render environment check failed", fields={"missing_filters": missing})
        return result

    section = config.section("video")
    limit = int(section.get("max_videos_per_run", 2))
    max_retries = int(section.get("max_retries", 3))

    rows = db.videos_pending_render(conn, limit=limit, max_retries=max_retries)
    for row in rows:
        result.outcomes.append(
            render_one_video(conn, row, config, work_dir, log, ydl_factory=ydl_factory)
        )

    log.info(
        "render pass complete",
        fields={"considered": len(rows), "rendered": result.rendered,
               "failed": len(result.failed)},
    )
    return result
