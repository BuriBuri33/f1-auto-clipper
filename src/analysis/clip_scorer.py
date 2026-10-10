"""The F1-weighted scoring formula (spec section 11).

    final_score = hook*0.25 + emotional*0.20 + context*0.15
                + payoff*0.20 + shareability*0.20

One function, applied identically to LLM-produced and heuristically-produced
sub-scores — which is what makes the two selection backends (clip_selector.py
and the heuristic in this file) genuinely interchangeable rather than
nominally so. The spec says the exact formula "can be changed after
testing"; the weights live in config, not as a hard-coded constant, for
exactly that reason.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from ..transcription.types import Segment
from .types import ClipCandidate, ScoredCandidate, SubScores

DEFAULT_WEIGHTS: Dict[str, float] = {
    "hook": 0.25,
    "emotional": 0.20,
    "context": 0.15,
    "payoff": 0.20,
    "shareability": 0.20,
}


def final_score(sub_scores: SubScores, weights: Optional[Dict[str, float]] = None) -> float:
    """Apply the weighted formula. Returns a float 0-100."""
    weights = weights or DEFAULT_WEIGHTS
    values = sub_scores.as_dict()
    missing = set(weights) - set(values)
    if missing:
        raise ValueError("weights reference unknown axes: %s" % sorted(missing))
    return sum(values[axis] * weight for axis, weight in weights.items())


def score_candidate(
    candidate: ClipCandidate, weights: Optional[Dict[str, float]] = None
) -> ScoredCandidate:
    return ScoredCandidate(candidate, final_score(candidate.sub_scores, weights))


def rank(
    candidates: Sequence[ClipCandidate], weights: Optional[Dict[str, float]] = None
) -> List[ScoredCandidate]:
    """Score and sort candidates, highest first."""
    scored = [score_candidate(c, weights) for c in candidates]
    scored.sort(key=lambda s: s.final_score, reverse=True)
    return scored


# --- the no-LLM heuristic fallback ------------------------------------------
#
# Deliberately not required to match LLM quality — it exists so the pipeline
# keeps running with no LLM_API_KEY, not to compete with one. Spec section 10
# is explicit that a clip must not be selected "solely because it contains an
# F1 keyword", so this scores on several independent signals and requires
# more than one to produce a high score, rather than keying off any single
# keyword hit.

# Team-radio / broadcast markers: a strong, fairly unambiguous signal that a
# moment was treated as significant by the people IN the broadcast, which is
# a better proxy than any topic keyword.
RADIO_MARKERS = (
    "box box box", "safety car", "virtual safety car", "yellow flag",
    "red flag", "penalty", "disqualified", "retire", "retirement",
    "crash", "collision", "spin", "lock up", "lockup", "puncture",
    "overtake", "overtaking", "pole position", "fastest lap",
)

# Emotional/exclamatory language — not F1-specific, which is the point: it
# transfers to any commentary-driven footage.
EMOTION_WORDS = (
    "incredible", "unbelievable", "amazing", "wow", "unreal", "insane",
    "disaster", "chaos", "drama", "brilliant", "massive", "huge",
    "controversial", "shocking", "stunning",
)


def _text_lower(segments: Sequence[Segment]) -> str:
    return " ".join(s.text for s in segments).lower()


def _count_hits(text: str, phrases: Sequence[str]) -> int:
    return sum(text.count(p) for p in phrases)


def heuristic_sub_scores(segments: Sequence[Segment]) -> SubScores:
    """Score one candidate's segments on the same five axes an LLM would use.

    Each axis is a simple, inspectable signal — not an attempt to approximate
    LLM judgement, which would be both inaccurate and unfalsifiable. The
    point is that the pipeline keeps moving without a key, with candidates
    that are at least ranked sensibly relative to each other.
    """
    text = _text_lower(segments)
    word_count = max(1, len(text.split()))

    radio_hits = _count_hits(text, RADIO_MARKERS)
    emotion_hits = _count_hits(text, EMOTION_WORDS)
    exclamations = text.count("!")
    questions = text.count("?")

    # hook: does it open with something that grabs attention? Radio markers
    # or emotional language in the first third weigh more.
    first_third = text[: max(1, len(text) // 3)]
    hook = min(100, 20 + 25 * _count_hits(first_third, RADIO_MARKERS)
              + 20 * _count_hits(first_third, EMOTION_WORDS)
              + 10 * (1 if "!" in first_third else 0))

    # emotional: density of emotional/exclamatory language across the whole span.
    emotional = min(100, 15 + 20 * emotion_hits + 12 * exclamations)

    # context: can it be understood standalone? Proxied by segment count —
    # a single very short segment lacks setup; several segments usually
    # means cause-and-effect (radio call -> reaction) is present.
    context = min(100, 30 + 15 * min(len(segments), 5))

    # payoff: a resolving marker (overtake completed, penalty confirmed,
    # crash, retirement) anywhere in the back half of the text.
    second_half = text[len(text) // 2 :]
    payoff = min(100, 20 + 30 * _count_hits(second_half, RADIO_MARKERS))

    # shareability: combination of brevity-appropriate density and emotional
    # language — a quieter proxy for "would this work with no context".
    shareability = min(100, 20 + 15 * radio_hits + 15 * emotion_hits
                       + 8 * (exclamations + questions))

    return SubScores(
        hook=int(hook), emotional=int(emotional), context=int(context),
        payoff=int(payoff), shareability=int(shareability),
    )


_WORD_RE = re.compile(r"[a-z0-9']+")


def word_set(text: str) -> set:
    return set(_WORD_RE.findall(text.lower()))


def jaccard_similarity(a: str, b: str) -> float:
    """0.0 (nothing shared) to 1.0 (identical word sets). Stdlib only."""
    wa, wb = word_set(a), word_set(b)
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
