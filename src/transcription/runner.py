"""Orchestrates download-then-transcribe as one pass ("ingest").

**Why these two stages are welded together.** A downloaded video is hundreds
of megabytes: far too large to commit, and the runner that downloaded it is
destroyed when the workflow ends. So the media exists only inside a single
workflow run, and anything that needs it must happen in that run. Transcribing
in a *later* run would mean downloading the video a second time.

So the durable artefacts of an ingest pass are the transcript (text, small,
committed) and the database row. The media is deliberately thrown away.

That is not a workaround for the free tier — it is the correct shape for a
pipeline whose compute is ephemeral and whose storage is a git repository.
Later phases consume the transcript, never the media, with one exception:
rendering (Phase 5) needs the video again, and will therefore re-download it
in its own run. Downloading twice is cheap; storing 200 MB per day is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..database import db
from ..download import runner as download_runner
from ..utils.config import REPO_ROOT, Config
from ..utils.logging import ContextLogger, get_logger
from . import transcriber as transcriber_module
from .types import Transcript, TranscriptionError, TranscriptionQuotaExceeded

TRANSCRIPT_DIR = REPO_ROOT / "data" / "transcripts"


@dataclass
class IngestOutcome:
    video_id: str
    title: str
    status: str
    reason: str = ""
    segments: Optional[int] = None
    backend: Optional[str] = None
    transcript_path: Optional[str] = None


@dataclass
class IngestResult:
    outcomes: List[IngestOutcome] = field(default_factory=list)
    downloaded: int = 0
    transcribed: int = 0
    api_units_spent: int = 0

    @property
    def failed(self) -> List[IngestOutcome]:
        return [o for o in self.outcomes if o.status == db.VideoStatus.FAILED.value]


def transcript_path_for(video_id: str, base: Optional[Path] = None) -> Path:
    base = Path(base) if base else TRANSCRIPT_DIR
    return base / ("%s.json" % video_id)


def transcribe_one(
    conn,
    row,
    media_path: Path,
    config: Config,
    log: ContextLogger,
    transcript_dir: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    api_key: Optional[str] = None,
    session=None,
    transcribe_fn=None,
) -> IngestOutcome:
    """Transcribe one already-downloaded video and record the result."""
    video_id = row["youtube_video_id"]
    vlog = log.bind(source_video=video_id)
    section = config.section("transcription")
    transcribe_fn = transcribe_fn or transcriber_module.transcribe

    try:
        transcript: Transcript = transcribe_fn(
            media_path,
            work_dir=work_dir or Path(media_path).parent,
            backend=str(section.get("backend", "auto")),
            api_key=api_key,
            groq_model=section.get("groq_model") or None,
            local_model=section.get("local_model") or None,
            language=section.get("language") or None,
            bitrate_kbps=int(section.get("bitrate_kbps", 32)),
            overlap_seconds=float(section.get("chunk_overlap_seconds", 4)),
            allow_local_fallback=bool(section.get("allow_local_fallback", True)),
            word_timestamps=bool(section.get("word_timestamps", True)),
            resegment=bool(section.get("resegment_from_words", True)),
            max_segment_seconds=float(section.get("max_segment_seconds", 8)),
            max_segment_chars=int(section.get("max_segment_chars", 180)),
            pause_threshold=float(section.get("pause_threshold_seconds", 0.8)),
            session=session,
            log=vlog,
        )
    except TranscriptionQuotaExceeded as exc:
        # Status unchanged: the video stays DOWNLOADED-but-untranscribed and
        # the next run retries. Never counted as a failure, because nothing
        # is wrong with the video or the code.
        vlog.warning("transcription deferred, free allowance spent",
                     fields={"reason": exc})
        return IngestOutcome(video_id, row["title"], "DEFERRED", str(exc))
    except TranscriptionError as exc:
        db.set_video_status(
            conn, row["id"], db.VideoStatus.FAILED,
            "transcription failed: %s" % exc, bump_retry=True,
        )
        vlog.warning("transcription failed", fields={"reason": exc})
        return IngestOutcome(video_id, row["title"], db.VideoStatus.FAILED.value,
                             str(exc))

    if transcript.is_empty:
        # A video with no speech cannot produce a captioned talking clip, and
        # this is terminal rather than retryable: re-running yields the same
        # silence.
        db.set_video_status(
            conn, row["id"], db.VideoStatus.SKIPPED,
            "transcript contains no speech", 
        )
        vlog.info("skipped, no speech found")
        return IngestOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value,
                             "transcript contains no speech")

    path = transcript_path_for(video_id, transcript_dir)
    transcript.write(path)
    db.record_transcript(
        conn,
        row["id"],
        path=str(path.relative_to(REPO_ROOT)) if _under(path, REPO_ROOT) else str(path),
        backend=transcript.backend or "unknown",
        model=transcript.model,
        language=transcript.language,
        segment_count=len(transcript.segments),
        covered_seconds=transcript.covered_seconds,
        chunk_count=transcript.chunk_count,
    )
    vlog.info(
        "transcribed",
        fields={"segments": len(transcript.segments),
                "backend": transcript.backend,
                "language": transcript.language,
                "chunks": transcript.chunk_count,
                "speech_s": round(transcript.covered_seconds, 1)},
    )
    return IngestOutcome(
        video_id, row["title"], db.VideoStatus.TRANSCRIBED.value,
        segments=len(transcript.segments), backend=transcript.backend,
        transcript_path=str(path),
    )


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def run_ingest(
    conn,
    config: Config,
    dest_dir: Path,
    log: Optional[ContextLogger] = None,
    transcript_dir: Optional[Path] = None,
    youtube_api_key: Optional[str] = None,
    transcription_api_key: Optional[str] = None,
    session=None,
    ydl_factory=None,
    transcribe_fn=None,
) -> IngestResult:
    """Download, then transcribe, in one pass over the pending videos."""
    log = log or get_logger(__name__)
    result = IngestResult()

    download_result = download_runner.run_download(
        conn, config, dest_dir=dest_dir, log=log,
        api_key=youtube_api_key, session=session, ydl_factory=ydl_factory,
    )
    result.api_units_spent = download_result.api_units_spent
    result.downloaded = len(download_result.downloaded)

    for outcome in download_result.outcomes:
        if outcome.status != db.VideoStatus.DOWNLOADED.value:
            result.outcomes.append(
                IngestOutcome(outcome.video_id, outcome.title, outcome.status,
                              outcome.reason)
            )
            continue

        row = db.get_video(conn, outcome.video_id)
        if row is None or not outcome.path:
            continue
        ingest_outcome = transcribe_one(
            conn, row, Path(outcome.path), config, log,
            transcript_dir=transcript_dir, work_dir=dest_dir,
            api_key=transcription_api_key, session=session,
            transcribe_fn=transcribe_fn,
        )
        result.outcomes.append(ingest_outcome)
        if ingest_outcome.status == db.VideoStatus.TRANSCRIBED.value:
            result.transcribed += 1

    log.info(
        "ingest pass complete",
        fields={"downloaded": result.downloaded,
                "transcribed": result.transcribed,
                "failed": len(result.failed)},
    )
    return result


def prune_transcripts(conn, transcript_dir: Optional[Path] = None) -> List[str]:
    """Delete transcripts for videos that have reached a terminal state.

    Transcripts are committed so they outlive the runner, which means they
    accumulate — roughly a few hundred kB per video. Once a video is
    PUBLISHED or SKIPPED its transcript has done its job.
    """
    removed: List[str] = []
    for row in db.transcripts_safe_to_prune(conn):
        recorded = row["transcript_path"]
        candidate = Path(recorded)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        if candidate.exists():
            candidate.unlink()
        db.clear_transcript(conn, row["id"])
        removed.append(row["youtube_video_id"])
    return removed
