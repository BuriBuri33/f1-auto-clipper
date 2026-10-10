"""Turn a Transcript into candidate clip windows (spec section 10).

Every window is built from whole segments, never a slice through one. This
is a direct payoff from Phase 3's decision to rebuild segments from word
timings rather than trust the backend's grouping: the resegmented transcript
already breaks on sentence endings and pauses (``resegment_from_words`` in
``transcription/types.py``), so a segment boundary IS a sentence or phrase
boundary. Building windows on segment boundaries is therefore what directly
satisfies "avoid clips that begin or end mid-sentence" — there is no separate
boundary-snapping step to get wrong.

Approach: anchor a window at every segment start, grow it by adding
consecutive segments until the span reaches ``target_duration`` (never
exceeding ``max_duration``), skip windows shorter than ``min_duration``, and
skip anchors that sit inside a pause larger than ``max_internal_gap`` (a
window should not silently swallow a long dead patch, since §10 asks us to
avoid clips containing long pauses).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from ..transcription.types import Segment


@dataclass(frozen=True)
class CandidateWindow:
    """A span of whole, contiguous segments — the unit analysis scores."""

    segments: List[Segment]

    @property
    def start(self) -> float:
        return self.segments[0].start

    @property
    def end(self) -> float:
        return self.segments[-1].end

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


def generate_windows(
    segments: Sequence[Segment],
    min_duration: float = 20.0,
    max_duration: float = 60.0,
    target_duration: float = 45.0,
    max_internal_gap: float = 6.0,
) -> List[CandidateWindow]:
    """Build every valid candidate window anchored at each segment.

    Returns one window per starting segment where a valid span exists — the
    caller (``runner.py``) is responsible for ranking and keeping only the
    best, non-overlapping subset (spec section 13: "very similar clips").
    Generating generously here and filtering later keeps this function pure
    and simple to test; filtering needs scores, which this function does not
    compute.
    """
    segments = [s for s in segments if s.text.strip()]
    if not segments:
        return []

    windows: List[CandidateWindow] = []
    n = len(segments)

    for start_index in range(n):
        window_segments = [segments[start_index]]
        for next_index in range(start_index + 1, n):
            gap = segments[next_index].start - window_segments[-1].end
            if gap > max_internal_gap:
                # A long dead patch — stop growing here rather than silently
                # swallowing it. What has been accumulated so far may still
                # be a valid (shorter) candidate, evaluated below.
                break

            span_if_added = segments[next_index].end - window_segments[0].start
            if span_if_added > max_duration:
                break
            window_segments.append(segments[next_index])

            span = window_segments[-1].end - window_segments[0].start
            if span >= target_duration:
                break

        span = window_segments[-1].end - window_segments[0].start
        if min_duration <= span <= max_duration:
            windows.append(CandidateWindow(list(window_segments)))

    return windows


def deduplicate_windows(
    windows: Sequence[CandidateWindow], overlap_threshold: float = 0.8
) -> List[CandidateWindow]:
    """Drop windows that are near-duplicates of a longer one already kept.

    ``generate_windows`` anchors at every segment, so a short run of three
    segments and the same three segments plus one more both appear. Kept
    simple and cheap: process longest-first, drop anything that overlaps an
    already-kept window by more than ``overlap_threshold`` of ITS OWN span —
    full scoring-aware dedup happens later in ``runner.py`` once sub-scores
    exist; this step only removes the most mechanical redundancy before
    spending LLM calls on it.
    """
    ordered = sorted(windows, key=lambda w: w.duration, reverse=True)
    kept: List[CandidateWindow] = []
    for window in ordered:
        is_redundant = False
        for existing in kept:
            overlap_start = max(window.start, existing.start)
            overlap_end = min(window.end, existing.end)
            overlap = max(0.0, overlap_end - overlap_start)
            if window.duration > 0 and (overlap / window.duration) >= overlap_threshold:
                is_redundant = True
                break
        if not is_redundant:
            kept.append(window)
    kept.sort(key=lambda w: w.start)
    return kept
