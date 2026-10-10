"""Candidate window generation — the code that decides clip boundaries.

Windows are built from whole, consecutive transcript segments. Because
Phase 3's resegment_from_words already breaks segments on sentence endings
and pauses, a segment boundary IS a sentence boundary — so building windows
on segment edges directly satisfies spec section 10's "avoid clips that
begin or end mid-sentence" without a separate boundary-snap step.
"""

import pytest

from src.analysis.candidates import deduplicate_windows, generate_windows
from src.transcription.types import Segment


def seg(start, end, text="x"):
    return Segment(start, end, text)


def test_a_single_long_segment_is_one_window():
    windows = generate_windows([seg(0, 30, "one long segment")],
                               min_duration=20, max_duration=60, target_duration=45)
    assert len(windows) == 1
    assert windows[0].start == 0 and windows[0].end == 30


def test_too_short_a_segment_produces_no_window():
    windows = generate_windows([seg(0, 5, "short")], min_duration=20, max_duration=60)
    assert windows == []


def test_consecutive_segments_are_combined_up_to_target():
    segments = [seg(0, 10, "a"), seg(10, 20, "b"), seg(20, 30, "c"), seg(30, 40, "d")]
    windows = generate_windows(segments, min_duration=20, max_duration=60,
                               target_duration=25)
    # Anchored at segment 0: grows until span >= target (25s), i.e. a+b+c = 30s.
    first = [w for w in windows if w.start == 0][0]
    assert first.end == 30
    assert [s.text for s in first.segments] == ["a", "b", "c"]


def test_a_window_never_exceeds_max_duration():
    segments = [seg(i * 10, i * 10 + 10, "s%d" % i) for i in range(10)]
    windows = generate_windows(segments, min_duration=20, max_duration=35,
                               target_duration=100)
    assert all(w.duration <= 35 for w in windows)


def test_a_long_internal_gap_stops_window_growth():
    """Spec section 10: avoid clips containing long pauses."""
    segments = [seg(0, 10, "a"), seg(10, 20, "b"), seg(50, 60, "c")]
    windows = generate_windows(segments, min_duration=5, max_duration=60,
                               target_duration=100, max_internal_gap=6)
    first = [w for w in windows if w.start == 0][0]
    # The gap from 20 to 50 (30s) must stop growth before segment c.
    assert first.end == 20
    assert [s.text for s in first.segments] == ["a", "b"]


def test_every_segment_is_a_potential_anchor():
    """Each starting point gets its own window, so later moments are not
    buried under one giant window anchored at the start."""
    segments = [seg(i * 10, i * 10 + 10, "s%d" % i) for i in range(6)]
    windows = generate_windows(segments, min_duration=5, max_duration=15,
                               target_duration=10)
    starts = {w.start for w in windows}
    assert starts == {s.start for s in segments}


def test_segments_with_blank_text_are_ignored():
    segments = [seg(0, 10, "real"), seg(10, 20, "   "), seg(20, 30, "also real")]
    windows = generate_windows(segments, min_duration=5, max_duration=60)
    all_text = " ".join(w.text for w in windows)
    assert "real" in all_text
    # Nothing crashes and no window is built solely from blank text.
    for w in windows:
        assert w.text.strip()


def test_empty_input_produces_no_windows():
    assert generate_windows([], min_duration=20, max_duration=60) == []


def test_window_text_joins_segment_text_with_whitespace_stripped():
    w = generate_windows([seg(0, 10, "  hello  "), seg(10, 25, "world  ")],
                         min_duration=20, max_duration=60, target_duration=30)[0]
    assert w.text == "hello world"


def test_window_boundaries_exactly_match_a_sentence_bounded_segment():
    """The payoff of building on segment edges: no mid-sentence cut."""
    segments = [seg(0.0, 3.68, "Lights out and away we go."),
               seg(7.68, 10.38, "Incredible overtake into turn one.")]
    windows = generate_windows(segments, min_duration=5, max_duration=60,
                               target_duration=15, max_internal_gap=10)
    combined = [w for w in windows if w.start == 0.0][0]
    assert combined.start == 0.0
    assert combined.end == 10.38
    assert combined.text.endswith("turn one.")


# --- deduplicate_windows ----------------------------------------------------


def test_deduplicate_drops_a_short_window_fully_inside_a_longer_one():
    segments = [seg(0, 10, "a"), seg(10, 20, "b"), seg(20, 30, "c")]
    windows = generate_windows(segments, min_duration=5, max_duration=60,
                               target_duration=100)
    deduped = deduplicate_windows(windows, overlap_threshold=0.8)
    # The longest window (a+b+c) should survive; windows nearly fully
    # contained in it should not also survive.
    assert any(w.start == 0 and w.end == 30 for w in deduped)
    assert len(deduped) < len(windows)


def test_deduplicate_keeps_genuinely_distinct_windows():
    segments = [seg(0, 10, "a"), seg(100, 110, "b")]
    windows = generate_windows(segments, min_duration=5, max_duration=60)
    deduped = deduplicate_windows(windows)
    assert len(deduped) == 2


def test_deduplicate_output_is_sorted_by_start():
    segments = [seg(i * 10, i * 10 + 8, "s%d" % i) for i in range(5)]
    windows = generate_windows(segments, min_duration=5, max_duration=15,
                               target_duration=8)
    deduped = deduplicate_windows(windows)
    assert [w.start for w in deduped] == sorted(w.start for w in deduped)


def test_deduplicate_empty_is_empty():
    assert deduplicate_windows([]) == []
