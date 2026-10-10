"""Orchestrates one analysis pass: transcript -> ranked, deduplicated clips.

Unlike ``download`` and ``ingest``, this stage reads only a committed
TRANSCRIPT, never the media — so it does not need to share a workflow job
with anything else. The media that produced the transcript is long gone by
the time this ever runs, which is exactly why Phases 2-3 were welded
together and Phase 4 is not.

Shape of a pass, per video:

    load transcript
        -> generate candidate windows (sentence/pause-bounded, spec section 10)
        -> drop mechanically-redundant windows
        -> score (LLM, or the heuristic fallback with no key)
        -> rank by the F1-weighted formula (spec section 11)
        -> keep the best NON-OVERLAPPING subset (spec section 13)
        -> drop anything textually near-duplicate to an already
           QUEUED/PUBLISHED clip from a DIFFERENT video
        -> store, capped at candidates_per_video

Status transitions:

    TRANSCRIBED  + candidates found  -> ANALYZED
    TRANSCRIBED  + LLM quota spent   -> unchanged (deferred, not a failure)
    TRANSCRIBED  + scoring failed    -> FAILED, retry_count bumped
    TRANSCRIBED  + zero candidates   -> SKIPPED (nothing met min_duration, or
                                        the transcript was too sparse — this
                                        is terminal: re-running on the same
                                        transcript yields the same nothing)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..database import db
from ..transcription.types import Transcript
from ..utils.config import REPO_ROOT, Config
from ..utils.logging import ContextLogger, get_logger
from . import candidates as candidates_module
from . import dedup
from .clip_scorer import DEFAULT_WEIGHTS, heuristic_sub_scores, rank
from .clip_selector import DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from .clip_selector import GeminiClipSelector
from .types import (
    AnalysisError,
    AnalysisQuotaExceeded,
    ClipCandidate,
    ScoredCandidate,
)

# A long video can produce far more candidate windows than are worth an LLM
# call over. Capped and evenly sampled across the timeline (never just the
# first N) so a 3-hour source is not scored only on its opening minutes.
DEFAULT_MAX_WINDOWS_FOR_LLM = 25


@dataclass
class VideoAnalysisOutcome:
    video_id: str
    title: str
    status: str
    reason: str = ""
    candidates: int = 0
    method: Optional[str] = None


@dataclass
class AnalysisRunResult:
    outcomes: List[VideoAnalysisOutcome] = field(default_factory=list)

    @property
    def analyzed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == db.VideoStatus.ANALYZED.value)

    @property
    def failed(self) -> List[VideoAnalysisOutcome]:
        return [o for o in self.outcomes if o.status == db.VideoStatus.FAILED.value]


def _resolve_transcript_path(recorded: str) -> Path:
    path = Path(recorded)
    return path if path.is_absolute() else REPO_ROOT / path


def _sample_evenly(items: List, cap: int) -> List:
    """Keep ``cap`` items spread across the sequence, not just the first ``cap``.

    Protects against a systematic bias toward a video's opening minutes when
    there are more candidate windows than the LLM batch cap allows.
    """
    if len(items) <= cap:
        return items
    step = len(items) / cap
    indices = sorted({int(i * step) for i in range(cap)})
    return [items[i] for i in indices]


def _score_heuristically(
    windows: List[candidates_module.CandidateWindow],
) -> List[ClipCandidate]:
    out = []
    for window in windows:
        sub_scores = heuristic_sub_scores(window.segments)
        out.append(
            ClipCandidate(
                start=window.start, end=window.end, sub_scores=sub_scores,
                reason="heuristic: keyword/structure signals, no LLM",
                hook=window.text[:80], text=window.text, method="heuristic",
            )
        )
    return out


def analyze_one_video(
    conn,
    row,
    config: Config,
    log: ContextLogger,
    api_key: Optional[str] = None,
    session=None,
    selector_factory=None,
) -> VideoAnalysisOutcome:
    """Analyze one TRANSCRIBED video and store its ranked candidates."""
    video_id = row["youtube_video_id"]
    vlog = log.bind(source_video=video_id)
    section = config.section("analysis")
    clip_cfg = config.section("clip")

    transcript_path = _resolve_transcript_path(row["transcript_path"])
    if not transcript_path.exists():
        return _fail(conn, row, vlog, "transcript file missing: %s" % transcript_path)

    try:
        transcript = Transcript.read(transcript_path)
    except (ValueError, OSError) as exc:
        return _fail(conn, row, vlog, "could not read transcript: %s" % exc)

    windows = candidates_module.generate_windows(
        transcript.segments,
        min_duration=float(clip_cfg.get("min_duration", 20)),
        max_duration=float(clip_cfg.get("max_duration", 60)),
        target_duration=float(clip_cfg.get("target_duration", 45)),
        max_internal_gap=float(section.get("max_internal_gap_seconds", 6)),
    )
    windows = candidates_module.deduplicate_windows(windows)

    if not windows:
        db.set_video_status(
            conn, row["id"], db.VideoStatus.SKIPPED,
            "no candidate window met the duration/pause constraints",
        )
        vlog.info("skipped, no viable candidate windows")
        return VideoAnalysisOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value,
                                    "no viable candidate windows")

    backend = str(section.get("backend", "auto"))
    if api_key is None:
        api_key = os.environ.get("LLM_API_KEY") or None

    use_llm = backend == "llm" or (backend == "auto" and api_key)
    method = "heuristic"
    candidates: List[ClipCandidate] = []

    if use_llm:
        if not api_key:
            return _fail(conn, row, vlog,
                         "analysis.backend is 'llm' but LLM_API_KEY is not set")
        selector = (selector_factory or GeminiClipSelector)(
            api_key=api_key,
            model=str(section.get("llm_model") or GEMINI_DEFAULT_MODEL),
            thinking_budget=int(section.get("thinking_budget", 0)),
            session=session,
        )
        max_windows = int(section.get("max_windows_for_llm", DEFAULT_MAX_WINDOWS_FOR_LLM))
        sampled = _sample_evenly(windows, max_windows)
        try:
            candidates = selector.score_windows(
                sampled, source_title=row["title"],
                vocabulary_hint=str(section.get("vocabulary_hint", "")),
            )
            method = "llm"
        except AnalysisQuotaExceeded as exc:
            vlog.warning("analysis deferred, free allowance spent", fields={"reason": exc})
            return VideoAnalysisOutcome(video_id, row["title"], "DEFERRED", str(exc))
        except AnalysisError as exc:
            if not bool(section.get("allow_heuristic_fallback", True)):
                return _fail(conn, row, vlog, "LLM scoring failed: %s" % exc)
            vlog.warning("LLM scoring failed, falling back to heuristic scoring",
                        fields={"error_type": type(exc).__name__, "error": exc})
            candidates = _score_heuristically(windows)
            method = "heuristic"
    else:
        candidates = _score_heuristically(windows)

    if not candidates:
        db.set_video_status(
            conn, row["id"], db.VideoStatus.SKIPPED,
            "scoring produced no valid candidates",
        )
        return VideoAnalysisOutcome(video_id, row["title"], db.VideoStatus.SKIPPED.value,
                                    "scoring produced no valid candidates", method=method)

    weights = section.get("scoring_weights") or DEFAULT_WEIGHTS
    ranked = rank(candidates, weights)
    non_overlapping = dedup.filter_overlapping(
        ranked, overlap_threshold=float(section.get("overlap_threshold", 0.5))
    )

    # Cross-video near-duplicate check: drop anything too similar to a clip
    # already queued or published from a DIFFERENT video.
    existing_texts = db.clip_texts_by_status(
        conn, (db.ClipStatus.QUEUED.value, db.ClipStatus.PUBLISHED.value)
    )
    similarity_threshold = float(
        section.get("similarity_threshold", dedup.DEFAULT_SIMILARITY_THRESHOLD)
    )
    final = [
        s for s in non_overlapping
        if not dedup.is_near_duplicate(s.candidate, existing_texts, similarity_threshold)
    ]

    if not final:
        # Every candidate survived SCORING but was then filtered out by
        # cross-video dedup — distinct from "scoring produced nothing"
        # above, and worth its own reason: an operator reading this status
        # needs to know it was a duplicate call, not a scoring failure.
        # Terminal for now; `unskip` can revisit it if the clip it was
        # judged a duplicate of is later rejected or un-queued.
        db.set_video_status(
            conn, row["id"], db.VideoStatus.SKIPPED,
            "every candidate was a near-duplicate of an already queued/"
            "published clip from a different video",
        )
        vlog.info("skipped, all candidates were cross-video duplicates")
        return VideoAnalysisOutcome(
            video_id, row["title"], db.VideoStatus.SKIPPED.value,
            "all candidates were cross-video duplicates", method=method,
        )

    cap = int(clip_cfg.get("candidates_per_video", 8))
    final = final[:cap]

    stored = db.record_clip_candidates(conn, row["id"], final)
    db.mark_video_analyzed(conn, row["id"], stored)
    vlog.info(
        "analyzed",
        fields={"candidates": stored, "method": method,
               "top_score": round(final[0].final_score, 1) if final else None},
    )
    return VideoAnalysisOutcome(
        video_id, row["title"], db.VideoStatus.ANALYZED.value,
        candidates=stored, method=method,
    )


def _fail(conn, row, vlog, reason: str) -> VideoAnalysisOutcome:
    db.set_video_status(conn, row["id"], db.VideoStatus.FAILED, reason, bump_retry=True)
    vlog.warning("analysis failed", fields={"reason": reason})
    return VideoAnalysisOutcome(
        row["youtube_video_id"], row["title"], db.VideoStatus.FAILED.value, reason
    )


def run_analysis(
    conn,
    config: Config,
    log: Optional[ContextLogger] = None,
    api_key: Optional[str] = None,
    session=None,
    selector_factory=None,
) -> AnalysisRunResult:
    """Run one analysis pass over every video pending analysis."""
    log = log or get_logger(__name__)
    section = config.section("analysis")
    limit = int(section.get("max_videos_per_run", 3))
    max_retries = int(section.get("max_retries", 3))

    result = AnalysisRunResult()
    rows = db.videos_pending_analysis(conn, limit=limit, max_retries=max_retries)
    for row in rows:
        result.outcomes.append(
            analyze_one_video(
                conn, row, config, log, api_key=api_key, session=session,
                selector_factory=selector_factory,
            )
        )

    log.info(
        "analysis pass complete",
        fields={"considered": len(rows), "analyzed": result.analyzed,
               "failed": len(result.failed)},
    )
    return result
