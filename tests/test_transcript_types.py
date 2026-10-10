import json

import pytest

from src.transcription.types import (
    Segment,
    Transcript,
    normalise,
)


def test_segment_rejects_an_inverted_span():
    with pytest.raises(ValueError, match="ends before it starts"):
        Segment(10.0, 5.0, "x")


def test_segment_geometry():
    s = Segment(10.0, 14.0, "hello")
    assert s.duration == 4.0
    assert s.midpoint == 12.0


def test_shifted_moves_both_ends_and_keeps_text():
    """The single most error-prone operation in the layer."""
    s = Segment(1.5, 3.5, "hello").shifted(100.0)
    assert (s.start, s.end, s.text) == (101.5, 103.5, "hello")


def test_shifted_by_zero_is_identity():
    s = Segment(1.5, 3.5, "hello")
    assert s.shifted(0.0) == s


def test_shifting_preserves_duration_exactly():
    for offset in (0.0, 1.0, 6250.0, 10800.0):
        assert Segment(1.0, 4.0, "x").shifted(offset).duration == pytest.approx(3.0)


def test_transcript_text_joins_non_empty_segments():
    t = Transcript([Segment(0, 1, "one"), Segment(1, 2, "  "), Segment(2, 3, "two")])
    assert t.text == "one two"


def test_transcript_is_empty_when_all_text_is_blank():
    assert Transcript([Segment(0, 1, "   ")]).is_empty is True
    assert Transcript([]).is_empty is True
    assert Transcript([Segment(0, 1, "a")]).is_empty is False


def test_covered_seconds_sums_durations():
    t = Transcript([Segment(0, 2, "a"), Segment(10, 13, "b")])
    assert t.covered_seconds == 5.0


def test_json_round_trip_preserves_everything(tmp_path):
    original = Transcript(
        segments=[Segment(1.2345, 4.5678, "hello there")],
        language="en", backend="groq", model="whisper-large-v3-turbo",
        source_duration=597.0, chunk_count=2,
    )
    path = original.write(tmp_path / "t.json")
    loaded = Transcript.read(path)

    assert loaded.language == "en"
    assert loaded.backend == "groq"
    assert loaded.chunk_count == 2
    assert loaded.source_duration == 597.0
    # Written rounded to milliseconds, which is ample for captions.
    assert loaded.segments[0].start == pytest.approx(1.2345, abs=0.001)
    assert loaded.segments[0].text == "hello there"


def test_the_on_disk_shape_matches_the_spec(tmp_path):
    """Spec section 9 fixes this shape; later phases depend on it."""
    path = Transcript([Segment(123.4, 128.7, "...")]).write(tmp_path / "t.json")
    payload = json.loads(path.read_text())
    assert "segments" in payload
    assert set(payload["segments"][0]) == {"start", "end", "text"}
    assert payload["segments"][0]["start"] == 123.4


def test_from_dict_rejects_a_payload_with_no_segments():
    with pytest.raises(ValueError, match="no 'segments' list"):
        Transcript.from_dict({"language": "en"})


def test_unicode_survives_the_round_trip(tmp_path):
    text = "Vai vai vai! Grazie ragazzi — что происходит? 🏁"
    path = Transcript([Segment(0, 1, text)]).write(tmp_path / "t.json")
    assert Transcript.read(path).segments[0].text == text


# --- normalise -------------------------------------------------------------


def test_normalise_sorts_out_of_order_segments():
    out = normalise([Segment(10, 12, "b"), Segment(1, 3, "a")])
    assert [s.text for s in out] == ["a", "b"]


def test_normalise_drops_blank_text():
    assert normalise([Segment(0, 1, "  "), Segment(1, 2, "keep")]) == [
        Segment(1, 2, "keep")
    ]


def test_normalise_drops_zero_length_segments():
    assert normalise([Segment(5, 5, "x")]) == []


def test_normalise_clamps_a_negative_start():
    assert normalise([Segment(-2, 3, "x")])[0].start == 0.0


def test_normalise_drops_a_hallucinated_tail_past_the_end():
    """Whisper routinely invents text after the audio ends."""
    out = normalise(
        [Segment(10, 12, "real"), Segment(700, 705, "hallucinated")],
        source_duration=600.0,
    )
    assert [s.text for s in out] == ["real"]


def test_normalise_clamps_a_segment_that_overshoots_slightly():
    out = normalise([Segment(595, 620, "x")], source_duration=600.0)
    assert out[0].end == pytest.approx(600.5)


def test_normalise_tolerates_a_frame_of_overshoot_without_dropping():
    out = normalise([Segment(599.9, 600.2, "x")], source_duration=600.0)
    assert len(out) == 1
