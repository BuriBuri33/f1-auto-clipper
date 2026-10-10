"""Shared types for clip analysis — candidates, scores, errors.

A ``ClipCandidate`` carries five F1-weighted sub-scores (spec section 11),
not just one opaque number, so the same ``final_score`` formula in
``clip_scorer.py`` applies identically whether the sub-scores came from the
LLM or from the no-LLM heuristic fallback. That symmetry is what lets the
two backends be genuinely interchangeable rather than nominally so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class AnalysisError(Exception):
    """Clip analysis could not be completed."""


class AnalysisQuotaExceeded(AnalysisError):
    """The LLM provider's free allowance is spent. Retry later, never pay."""


@dataclass(frozen=True)
class SubScores:
    """The five axes spec section 11 weights into one final score.

    Each is 0-100. Kept as a dataclass rather than a plain dict so a missing
    or out-of-range axis is a constructor error, not a silent `.get(..., 0)`
    default that would quietly understate a clip's score.
    """

    hook: int
    emotional: int
    context: int
    payoff: int
    shareability: int

    def __post_init__(self) -> None:
        for name in ("hook", "emotional", "context", "payoff", "shareability"):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= 100:
                raise ValueError(
                    "SubScores.%s must be an int 0-100, got %r" % (name, value)
                )

    def as_dict(self) -> Dict[str, int]:
        return {
            "hook": self.hook, "emotional": self.emotional,
            "context": self.context, "payoff": self.payoff,
            "shareability": self.shareability,
        }


@dataclass(frozen=True)
class ClipCandidate:
    """One candidate clip: a timestamp span, sub-scores, and provenance.

    ``start``/``end`` are float seconds on the SOURCE video's timeline — the
    same convention as ``transcription.types.Segment``, so no conversion is
    needed between the stage that reads transcripts and the stage that
    writes clips (the clips TABLE stores integer milliseconds, for the
    different reason that a UNIQUE constraint over floats is unreliable;
    that conversion happens once, at the database boundary).
    """

    start: float
    end: float
    sub_scores: SubScores
    reason: str
    hook: str
    text: str
    method: str                       # "llm" | "heuristic"
    model: Optional[str] = None

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                "candidate ends before it starts: %.2f -> %.2f"
                % (self.start, self.end)
            )

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "ClipCandidate", threshold: float = 0.5) -> bool:
        """Whether this candidate overlaps ``other`` by at least ``threshold``
        of the shorter candidate's duration.

        Used to keep one video's candidate set from being dominated by near-
        duplicate windows around the same moment (spec section 13, "very
        similar clips").
        """
        overlap_start = max(self.start, other.start)
        overlap_end = min(self.end, other.end)
        overlap = max(0.0, overlap_end - overlap_start)
        shorter = min(self.duration, other.duration)
        if shorter <= 0:
            return False
        return (overlap / shorter) >= threshold


@dataclass
class ScoredCandidate:
    """A candidate plus its computed final_score, ready to store or rank."""

    candidate: ClipCandidate
    final_score: float

    @property
    def start(self) -> float:
        return self.candidate.start

    @property
    def end(self) -> float:
        return self.candidate.end
