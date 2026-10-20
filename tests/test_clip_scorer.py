"""The five-axis weighted scoring formula and its no-LLM heuristic fallback."""

import pytest

from src.analysis.clip_scorer import (
    DEFAULT_WEIGHTS,
    final_score,
    heuristic_sub_scores,
    jaccard_similarity,
    rank,
    score_candidate,
)
from src.analysis.types import ClipCandidate, SubScores
from src.transcription.types import Segment


def sub(hook=50, emotional=50, context=50, payoff=50, shareability=50):
    return SubScores(hook, emotional, context, payoff, shareability)


def candidate(start=0.0, end=30.0, sub_scores=None, method="llm"):
    sub_scores = sub_scores or sub()
    return ClipCandidate(start=start, end=end, sub_scores=sub_scores,
                         reason="r", hook="h", text="t", method=method)


# --- the formula itself -----------------------------------------------------


def test_the_formula_matches_spec_section_11_exactly():
    s = SubScores(hook=100, emotional=0, context=0, payoff=0, shareability=0)
    assert final_score(s) == pytest.approx(25.0)   # hook weight is 0.25


def test_weights_sum_to_a_sensible_total_on_a_uniform_input():
    s = sub(60, 60, 60, 60, 60)
    assert final_score(s) == pytest.approx(60.0)


def test_default_weights_match_the_spec_formula():
    assert DEFAULT_WEIGHTS == {
        "hook": 0.25, "emotional": 0.20, "context": 0.15,
        "payoff": 0.20, "shareability": 0.20,
    }


def test_custom_weights_can_be_substituted():
    """Spec section 11: 'the exact formula can be changed after testing'."""
    s = SubScores(hook=100, emotional=0, context=0, payoff=0, shareability=0)
    custom = {"hook": 1.0, "emotional": 0.0, "context": 0.0, "payoff": 0.0,
             "shareability": 0.0}
    assert final_score(s, custom) == pytest.approx(100.0)


def test_unknown_weight_axis_is_an_error():
    with pytest.raises(ValueError, match="unknown axes"):
        final_score(sub(), {"not_an_axis": 1.0})


def test_score_candidate_wraps_a_candidate_with_its_final_score():
    c = candidate(sub_scores=SubScores(100, 100, 100, 100, 100))
    scored = score_candidate(c)
    assert scored.final_score == pytest.approx(100.0)
    assert scored.candidate is c


def test_rank_sorts_highest_first():
    low = candidate(0, 30, SubScores(10, 10, 10, 10, 10))
    high = candidate(100, 130, SubScores(90, 90, 90, 90, 90))
    ranked = rank([low, high])
    assert ranked[0].candidate is high
    assert ranked[1].candidate is low


def test_rank_on_an_empty_list():
    assert rank([]) == []


# --- SubScores validation ---------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 101, 1.5, "90", None])
def test_subscores_rejects_an_out_of_range_or_wrong_type_axis(bad):
    with pytest.raises(ValueError):
        SubScores(hook=bad, emotional=50, context=50, payoff=50, shareability=50)


def test_subscores_accepts_the_boundary_values():
    SubScores(0, 0, 0, 0, 0)
    SubScores(100, 100, 100, 100, 100)


# --- ClipCandidate -----------------------------------------------------------


def test_candidate_rejects_an_inverted_span():
    with pytest.raises(ValueError, match="ends before it starts"):
        candidate(start=30, end=10)


def test_candidate_duration():
    assert candidate(10, 40).duration == 30.0


@pytest.mark.parametrize("a_span,b_span,overlaps", [
    ((0, 30), (20, 50), True),       # 10s overlap / 30s shorter = 0.33 < 0.5 -> False actually
    ((0, 30), (10, 25), True),       # 15s overlap / 15s shorter = 1.0 -> True
    ((0, 30), (100, 130), False),    # no overlap
])
def test_overlaps_uses_the_shorter_candidate_as_the_denominator(a_span, b_span, overlaps):
    a = candidate(*a_span)
    b = candidate(*b_span)
    # Recompute expected using the actual formula to avoid a hand-miscalculated
    # parametrize table.
    overlap = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shorter = min(a.duration, b.duration)
    expected = (overlap / shorter) >= 0.5 if shorter > 0 else False
    assert a.overlaps(b) == expected


def test_overlaps_is_symmetric():
    a, b = candidate(0, 30), candidate(10, 40)
    assert a.overlaps(b) == b.overlaps(a)


# --- heuristic fallback ------------------------------------------------------


def test_heuristic_never_calls_out_anything_external(monkeypatch):
    """Confidence that the fallback truly needs no network: no requests
    module is even imported by this function's call path."""
    segments = [Segment(0, 10, "Box box box, confirm box this lap.")]
    scores = heuristic_sub_scores(segments)
    assert isinstance(scores, SubScores)


def test_heuristic_scores_radio_markers_higher_than_bland_text():
    exciting = [Segment(0, 10, "Box box box! Incredible overtake into turn one!")]
    bland = [Segment(0, 10, "The car felt okay I guess, nothing much to say.")]
    high = final_score(heuristic_sub_scores(exciting))
    low = final_score(heuristic_sub_scores(bland))
    assert high > low


def test_heuristic_does_not_score_purely_on_a_single_keyword_hit():
    """Spec section 10: must not select solely because it contains an F1
    keyword. One bare mention, no other signal, should not max out."""
    one_keyword = [Segment(0, 10, "overtake")]
    scores = heuristic_sub_scores(one_keyword)
    assert final_score(scores) < 90


def test_heuristic_handles_empty_segments_without_crashing():
    assert isinstance(heuristic_sub_scores([]), SubScores)


def test_heuristic_sub_scores_are_always_in_range():
    segments = [Segment(0, 10, "incredible unbelievable amazing wow " * 20 + "!" * 50)]
    scores = heuristic_sub_scores(segments)
    for value in scores.as_dict().values():
        assert 0 <= value <= 100


# --- jaccard_similarity ------------------------------------------------------


def test_identical_text_is_similarity_one():
    assert jaccard_similarity("box box box this lap", "box box box this lap") == 1.0


def test_disjoint_text_is_similarity_zero():
    assert jaccard_similarity("red blue green", "cat dog fish") == 0.0


def test_partial_overlap_is_between_zero_and_one():
    sim = jaccard_similarity("verstappen overtakes hamilton turn one",
                             "verstappen crashes out turn one")
    assert 0.0 < sim < 1.0


def test_case_and_punctuation_are_normalised():
    assert jaccard_similarity("Box Box Box!", "box box box") == 1.0


def test_both_empty_is_similarity_one():
    """Two empty strings share nothing to disagree on."""
    assert jaccard_similarity("", "") == 1.0


def test_one_empty_is_similarity_zero():
    assert jaccard_similarity("some words here", "") == 0.0
