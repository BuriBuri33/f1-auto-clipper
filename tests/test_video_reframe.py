import pytest

from src.video import reframe
from src.video.types import ReframeDecision


def test_crop_box_centres_a_16_9_source_onto_9_16():
    box = reframe.crop_box(1920, 1080, 1080, 1920)
    assert box is not None
    cw, ch, cx, cy = box
    assert ch == 1080
    assert cw == pytest.approx(1080 * 9 / 16, abs=2)
    # Centred: equal margin on both sides.
    assert cx == pytest.approx((1920 - cw) / 2, abs=2)
    assert cy == 0


def test_crop_box_dimensions_are_even():
    cw, ch, cx, cy = reframe.crop_box(1921, 1081, 1080, 1920)
    assert cw % 2 == 0
    assert ch % 2 == 0
    assert cx % 2 == 0


def test_crop_box_is_none_for_a_source_already_narrower_than_the_target():
    # 300x600 is aspect 0.5, narrower than 9:16 (0.5625): cropping the width
    # down further is impossible.
    assert reframe.crop_box(300, 600, 1080, 1920) is None


def test_decide_uses_center_crop_for_a_normal_16_9_source():
    decision = reframe.decide("center_crop", 1920, 1080, 1080, 1920)
    assert decision.strategy == "center_crop"


def test_decide_falls_back_to_blur_pad_for_a_degenerate_crop():
    decision = reframe.decide("center_crop", 300, 600, 1080, 1920)
    assert decision.strategy == "blur_pad"
    assert "negative width" in decision.reason


def test_decide_honours_an_explicit_blur_pad_configuration():
    decision = reframe.decide("blur_pad", 1920, 1080, 1080, 1920)
    assert decision.strategy == "blur_pad"
    assert decision.reason == "configured"


def test_decide_rejects_an_unknown_strategy():
    with pytest.raises(ValueError):
        reframe.decide("fisheye", 1920, 1080, 1080, 1920)


def test_filter_clause_center_crop_is_a_simple_chain():
    decision = ReframeDecision("center_crop", "configured")
    clause = reframe.filter_clause(decision, 1920, 1080, 1080, 1920)
    assert clause.startswith("[0:v]crop=")
    assert "scale=1080:1920" in clause
    assert "split" not in clause


def test_filter_clause_blur_pad_splits_into_background_and_foreground():
    decision = ReframeDecision("blur_pad", "configured")
    clause = reframe.filter_clause(decision, 1920, 1080, 1080, 1920)
    assert "split=2[bg][fg]" in clause
    assert "gblur" in clause
    assert "overlay=" in clause


def test_filter_clause_raises_if_asked_for_an_impossible_center_crop():
    """decide() would have fallen back; calling filter_clause directly with a
    center_crop decision on degenerate geometry is a programming error, not a
    runtime condition to recover from."""
    decision = ReframeDecision("center_crop", "configured")
    with pytest.raises(ValueError):
        reframe.filter_clause(decision, 300, 600, 1080, 1920)


def test_filter_clause_uses_the_given_input_label():
    decision = ReframeDecision("center_crop", "configured")
    clause = reframe.filter_clause(decision, 1920, 1080, 1080, 1920, input_label="v5")
    assert clause.startswith("[v5]crop=")
