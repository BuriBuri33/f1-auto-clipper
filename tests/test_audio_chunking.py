"""Chunk planning and timestamp re-timing.

This is the file that matters most in Phase 3. A chunking bug does not crash:
it produces a complete, fluent, plausible transcript in which everything after
the first boundary is at the wrong time, and captions drift by minutes. So
these tests check the arithmetic directly, with no encoding involved.
"""

import pytest

from src.transcription.audio import (
    AudioChunk,
    AudioError,
    SIZE_SAFETY_FACTOR,
    bytes_per_second,
    merge_chunk_segments,
    plan_chunks,
)
from src.transcription.types import Segment

MB = 1024 * 1024


def chunks_from_plan(plans, paths=None):
    return [
        AudioChunk(paths[i] if paths else "chunk%d.mp3" % i, i, start, dur, ws, we)
        for i, (start, dur, ws, we) in enumerate(plans)
    ]


# --- planning --------------------------------------------------------------


def test_the_rate_comes_from_the_nominal_bitrate_not_a_measured_file():
    """32 kbit/s is 4,000 bytes/s. Measured averages mislead: a mostly-silent
    file averaged 2,544 B/s while its chunks encoded at 4,040 B/s — 43% over
    the predicted size, and over the provider's cap."""
    assert bytes_per_second(32) == 4000.0
    assert bytes_per_second(64) == 8000.0


def test_audio_inside_the_limit_is_one_chunk():
    plans = plan_chunks(duration=600.0, max_bytes=25 * MB, bitrate_kbps=32)
    assert plans == [(0.0, 600.0, 0.0, 600.0)]


def test_audio_exactly_at_the_safety_budget_is_one_chunk():
    budget = int(25 * MB * SIZE_SAFETY_FACTOR)
    duration = budget / bytes_per_second(32)
    assert len(plan_chunks(duration, 25 * MB, 32)) == 1


def test_a_three_hour_source_at_32kbps_splits_into_two_chunks():
    """The real case the 25 MB cap forces: 4 kB/s, so ~104 min per chunk."""
    plans = plan_chunks(3 * 3600.0, max_bytes=25 * MB, bitrate_kbps=32, overlap=4.0)
    assert len(plans) == 2


def test_every_chunk_fits_under_the_limit():
    rate = bytes_per_second(32)
    plans = plan_chunks(3 * 3600.0, 25 * MB, 32, overlap=4.0)
    for _start, chunk_duration, _ws, _we in plans:
        assert chunk_duration * rate <= 25 * MB


def test_a_higher_bitrate_forces_more_chunks():
    low = plan_chunks(3 * 3600.0, 25 * MB, 32, overlap=4.0)
    high = plan_chunks(3 * 3600.0, 25 * MB, 128, overlap=4.0)
    assert len(high) > len(low)


def test_chunks_cover_the_whole_duration_with_no_gaps():
    """The windows must tile the timeline exactly — no gap, no overlap."""
    duration = 3 * 3600.0
    plans = plan_chunks(duration, 25 * MB, 32, overlap=4.0)
    windows = [(ws, we) for _s, _d, ws, we in plans]

    assert windows[0][0] == 0.0
    assert windows[-1][1] == pytest.approx(duration)
    for earlier, later in zip(windows, windows[1:]):
        assert earlier[1] == pytest.approx(later[0]), "windows must meet exactly"


def test_chunks_physically_overlap_even_though_windows_do_not():
    """Overlapping audio is what stops a word being cut in half; the windows
    then decide who owns it."""
    duration = 3 * 3600.0
    plans = plan_chunks(duration, 25 * MB, 32, overlap=4.0)
    first_end = plans[0][0] + plans[0][1]
    second_start = plans[1][0]
    assert second_start < first_end
    assert first_end - second_start == pytest.approx(4.0)


def test_many_chunks_still_tile_exactly():
    """A low limit forces many boundaries — the stress case for the tiling."""
    duration = 10800.0
    plans = plan_chunks(duration, 2 * MB, 32, overlap=4.0)
    assert len(plans) > 5
    assert plans[0][2] == 0.0
    assert plans[-1][3] == pytest.approx(duration)
    for (_s1, _d1, _w1, end), (_s2, _d2, start, _w2) in zip(plans, plans[1:]):
        assert end == pytest.approx(start)


def test_zero_duration_is_refused():
    with pytest.raises(AudioError, match="duration"):
        plan_chunks(0.0, 25 * MB, 32)


def test_a_non_positive_bitrate_is_refused():
    with pytest.raises(AudioError, match="bitrate"):
        plan_chunks(600.0, 25 * MB, 0)


def test_a_limit_too_small_to_overlap_is_refused_with_advice():
    """Better to refuse than to emit chunks shorter than their own overlap."""
    with pytest.raises(AudioError, match="bitrate_kbps"):
        plan_chunks(3600.0, max_bytes=20_000, bitrate_kbps=32, overlap=4.0)


# --- merging and re-timing -------------------------------------------------


def test_segments_are_shifted_onto_the_source_timeline():
    plans = plan_chunks(7200.0, 25 * MB, 32, overlap=4.0)
    chunks = chunks_from_plan(plans)
    assert len(chunks) == 2

    # Each backend reports times relative to the chunk it was handed.
    per_chunk = [
        [Segment(10.0, 13.0, "first chunk")],
        [Segment(20.0, 23.0, "second chunk")],
    ]
    merged = merge_chunk_segments(chunks, per_chunk)

    assert merged[0].start == pytest.approx(10.0)
    # The second chunk's segment must land at its SOURCE time, not at 20s.
    expected = chunks[1].source_start + 20.0
    assert merged[1].start == pytest.approx(expected)
    # The failure this guards against is a forgotten offset, which would
    # leave the segment near 20s instead of ~98 minutes in.
    assert merged[1].start > chunks[1].source_start
    assert merged[1].start - 20.0 == pytest.approx(chunks[1].source_start)


def test_a_single_chunk_needs_no_shift():
    chunks = [AudioChunk("a.mp3", 0, 0.0, 600.0, 0.0, 600.0)]
    merged = merge_chunk_segments(chunks, [[Segment(5.0, 7.0, "x")]])
    assert merged[0].start == 5.0


def test_overlapping_speech_is_kept_exactly_once():
    """Both chunks transcribe the shared audio; only one may keep it."""
    chunks = [
        AudioChunk("a.mp3", 0, 0.0, 100.0, 0.0, 98.0),
        AudioChunk("b.mp3", 1, 96.0, 100.0, 98.0, 196.0),
    ]
    # The same utterance at source time 97-99, seen by both chunks.
    per_chunk = [
        [Segment(97.0, 99.0, "shared")],              # chunk-relative == source
        [Segment(1.0, 3.0, "shared")],                # 96 + 1 = 97
    ]
    merged = merge_chunk_segments(chunks, per_chunk)
    assert [s.text for s in merged] == ["shared"]


def test_a_segment_straddling_the_boundary_goes_to_whoever_holds_most_of_it():
    chunks = [
        AudioChunk("a.mp3", 0, 0.0, 100.0, 0.0, 98.0),
        AudioChunk("b.mp3", 1, 96.0, 100.0, 98.0, 196.0),
    ]
    # Midpoint 97.5 -> belongs to the first chunk's window.
    early = merge_chunk_segments(chunks, [[Segment(96.0, 99.0, "early")], []])
    assert [s.text for s in early] == ["early"]

    # Midpoint 99.5 -> belongs to the second chunk's window.
    late = merge_chunk_segments(chunks, [[], [Segment(2.5, 5.5, "late")]])
    assert [s.text for s in late] == ["late"]


def test_nothing_is_lost_across_a_boundary():
    """Speech every second across a boundary must survive exactly once each."""
    chunks = [
        AudioChunk("a.mp3", 0, 0.0, 100.0, 0.0, 98.0),
        AudioChunk("b.mp3", 1, 96.0, 100.0, 98.0, 196.0),
    ]
    first = [Segment(float(t), float(t) + 1, "t%d" % t) for t in range(0, 100)]
    second = [Segment(float(t), float(t) + 1, "t%d" % (t + 96))
              for t in range(0, 100)]
    merged = merge_chunk_segments(chunks, [first, second])

    texts = [s.text for s in merged]
    assert len(texts) == len(set(texts)), "no utterance may appear twice"
    # Every whole second from 0 to 195 is accounted for.
    assert len(merged) == 196


def test_merged_output_is_sorted():
    chunks = [
        AudioChunk("a.mp3", 0, 0.0, 100.0, 0.0, 98.0),
        AudioChunk("b.mp3", 1, 96.0, 100.0, 98.0, 196.0),
    ]
    merged = merge_chunk_segments(
        chunks, [[Segment(50, 52, "a")], [Segment(10, 12, "b")]]
    )
    assert [s.start for s in merged] == sorted(s.start for s in merged)


def test_mismatched_chunk_and_segment_counts_is_an_error():
    """Silently zipping to the shorter list would drop a chunk's speech."""
    chunks = [AudioChunk("a.mp3", 0, 0.0, 100.0, 0.0, 100.0)]
    with pytest.raises(AudioError, match="2 segment lists"):
        merge_chunk_segments(chunks, [[], []])


def test_an_empty_chunk_contributes_nothing_and_breaks_nothing():
    chunks = [
        AudioChunk("a.mp3", 0, 0.0, 100.0, 0.0, 98.0),
        AudioChunk("b.mp3", 1, 96.0, 100.0, 98.0, 196.0),
    ]
    merged = merge_chunk_segments(chunks, [[], [Segment(10, 12, "only")]])
    assert [s.text for s in merged] == ["only"]
