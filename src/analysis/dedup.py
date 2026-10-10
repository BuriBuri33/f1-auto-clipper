"""Clip deduplication beyond the database's own uniqueness constraints.

The schema already makes two kinds of duplicate impossible by construction
(spec section 13):

* ``UNIQUE(source_video_id, start_ms, end_ms)`` — the exact same span from
  the same video can only be stored once.
* ``UNIQUE(clip_id, platform)`` — a clip can only be published once per
  platform.

What the schema cannot catch is a NEAR-duplicate: the same moment described
in slightly different words across two different source videos (a race
re-cap repeating a clip from the live broadcast, for instance). That is
checked here with a cheap, stdlib-only text-similarity measure — Jaccard
similarity over word sets — against every clip already QUEUED or PUBLISHED.

This is deliberately crude. A proper near-duplicate detector would need
embeddings or fuzzy matching, which is a real dependency for a check that,
at one clip a day, runs a handful of times. Crude-but-cheap is the right
trade here; perceptual hashing (spec section 13's other suggestion) applies
to the RENDERED video, which does not exist until Phase 5, and belongs
there if it is ever added.
"""

from __future__ import annotations

from typing import List, Sequence

from .clip_scorer import jaccard_similarity
from .types import ClipCandidate, ScoredCandidate

DEFAULT_SIMILARITY_THRESHOLD = 0.6


def is_near_duplicate(
    candidate: ClipCandidate,
    existing_texts: Sequence[str],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> bool:
    """Whether ``candidate`` is textually too similar to something already queued."""
    return any(
        jaccard_similarity(candidate.text, existing) >= threshold
        for existing in existing_texts
        if existing
    )


def filter_overlapping(
    scored: Sequence[ScoredCandidate], overlap_threshold: float = 0.5
) -> List[ScoredCandidate]:
    """Keep the best-scoring candidate from each cluster of overlapping ones.

    Input must already be sorted best-first (``clip_scorer.rank`` does this)
    — a single pass with no re-sorting, and "best" is unambiguous: whichever
    one is encountered first wins, which is correct precisely because the
    input order already reflects final_score.
    """
    kept: List[ScoredCandidate] = []
    for item in scored:
        if any(item.candidate.overlaps(k.candidate, overlap_threshold) for k in kept):
            continue
        kept.append(item)
    return kept
