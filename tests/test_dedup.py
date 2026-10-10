"""Cross-video near-duplicate detection and overlap filtering (spec section 13)."""

import pytest

from src.analysis.dedup import filter_overlapping, is_near_duplicate
from src.analysis.clip_scorer import score_candidate
from src.analysis.types import ClipCandidate, SubScores


def sub(v=50):
    return SubScores(v, v, v, v, v)


def candidate(start, end, text="t", score_value=50):
    return ClipCandidate(start=start, end=end, sub_scores=sub(score_value),
                         reason="r", hook="h", text=text, method="llm")


# --- is_near_duplicate -------------------------------------------------------


def test_identical_text_is_a_near_duplicate():
    c = candidate(0, 30, text="verstappen overtakes hamilton into turn one")
    assert is_near_duplicate(c, ["verstappen overtakes hamilton into turn one"])


def test_unrelated_text_is_not_a_near_duplicate():
    c = candidate(0, 30, text="verstappen overtakes hamilton")
    assert not is_near_duplicate(c, ["completely different weather commentary"])


def test_empty_existing_pool_means_nothing_is_a_duplicate():
    c = candidate(0, 30, text="anything at all")
    assert not is_near_duplicate(c, [])


def test_blank_existing_entries_are_skipped_not_matched():
    c = candidate(0, 30, text="box box box")
    assert not is_near_duplicate(c, ["", None, ""])


def test_threshold_is_respected():
    c = candidate(0, 30, text="a b c d e")
    somewhat_similar = "a b x y z"       # 2/8 words shared = 0.25
    assert not is_near_duplicate(c, [somewhat_similar], threshold=0.6)
    assert is_near_duplicate(c, [somewhat_similar], threshold=0.2)


# --- filter_overlapping ------------------------------------------------------


def test_non_overlapping_candidates_are_all_kept():
    scored = [score_candidate(candidate(0, 30)), score_candidate(candidate(100, 130))]
    assert len(filter_overlapping(scored)) == 2


def test_the_higher_ranked_candidate_wins_an_overlap():
    high = score_candidate(candidate(0, 30, score_value=90))
    low = score_candidate(candidate(10, 40, score_value=20))
    # Caller must sort best-first before filtering.
    kept = filter_overlapping([high, low])
    assert kept == [high]


def test_order_determines_the_winner_not_the_score_field_itself():
    """filter_overlapping trusts input order; it does not re-sort. Passing
    the lower one first means IT wins, which is why callers must rank first."""
    a = score_candidate(candidate(0, 30, score_value=20))
    b = score_candidate(candidate(10, 40, score_value=90))
    kept = filter_overlapping([a, b])
    assert kept == [a]


def test_a_chain_of_three_overlapping_candidates_drops_only_the_middle():
    """b overlaps a (15-30 shared / 30s = 0.5), so b is dropped. c touches a
    at a single point (30-30 = zero overlap), so c is NOT redundant with a
    and survives — overlap, not adjacency, is what filter_overlapping removes."""
    a = score_candidate(candidate(0, 30))
    b = score_candidate(candidate(15, 45))
    c = score_candidate(candidate(30, 60))
    kept = filter_overlapping([a, b, c], overlap_threshold=0.3)
    assert kept == [a, c]


def test_overlap_threshold_is_configurable():
    a = score_candidate(candidate(0, 30))
    b = score_candidate(candidate(25, 55))     # 5s overlap / 30s = 0.17
    assert filter_overlapping([a, b], overlap_threshold=0.1) == [a]
    assert len(filter_overlapping([a, b], overlap_threshold=0.5)) == 2


def test_empty_input_is_empty_output():
    assert filter_overlapping([]) == []


def test_a_single_candidate_is_always_kept():
    only = score_candidate(candidate(0, 30))
    assert filter_overlapping([only]) == [only]
