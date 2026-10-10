import pytest

from src.transcription.types import Segment, Word
from src.video import captions


def words(start, text_and_dur):
    """Build consecutive words starting at ``start``, each ``(text, duration)``."""
    out = []
    t = start
    for text, dur in text_and_dur:
        out.append(Word(t, t + dur, text))
        t += dur
    return out


def test_build_cues_shifts_timestamps_to_clip_relative():
    seg_words = words(10.0, [("Box", 0.3), ("box", 0.3), ("box.", 0.4)])
    segment = Segment(10.0, 11.0, "Box box box.", seg_words)
    cues = captions.build_cues([segment], clip_start=10.0, clip_end=20.0)
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(0.0, abs=0.01)
    assert cues[0].text == "Box box box."


def test_build_cues_drops_words_outside_the_clip_window():
    seg_words = words(0.0, [("Before.", 0.5)]) + words(10.0, [("Inside.", 0.5)]) + words(25.0, [("After.", 0.5)])
    segment = Segment(0.0, 25.5, "Before. Inside. After.", seg_words)
    cues = captions.build_cues([segment], clip_start=10.0, clip_end=20.0)
    text = " ".join(c.text for c in cues)
    assert "Before" not in text
    assert "After" not in text
    assert "Inside." in text


def test_build_cues_keeps_a_whole_wordless_segment_inside_the_window():
    segment = Segment(12.0, 14.0, "No word timings here.")
    cues = captions.build_cues([segment], clip_start=10.0, clip_end=20.0)
    assert len(cues) == 1
    assert cues[0].start == pytest.approx(2.0)
    assert cues[0].end == pytest.approx(4.0)


def test_build_cues_drops_a_wordless_segment_that_straddles_the_boundary():
    segment = Segment(8.0, 14.0, "Straddles the start.")
    cues = captions.build_cues([segment], clip_start=10.0, clip_end=20.0)
    assert cues == []


def test_build_cues_splits_long_speech_into_several_cues():
    long_text = [("one", 0.3), ("two", 0.3), ("three", 0.3), ("four", 0.3),
                 ("five", 0.3), ("six", 0.3), ("seven", 0.3), ("eight", 0.3)]
    segment = Segment(0.0, 2.4, " ".join(w for w, _ in long_text), words(0.0, long_text))
    cues = captions.build_cues(
        [segment], clip_start=0.0, clip_end=10.0, max_chars=12, max_seconds=100,
    )
    assert len(cues) > 1
    assert all(len(c.text) <= 20 for c in cues)  # some slack over max_chars for the last word


def test_cue_end_is_clamped_to_clip_duration():
    seg_words = words(18.0, [("Over", 1.0), ("the", 1.0), ("edge.", 1.0)])
    segment = Segment(18.0, 21.0, "Over the edge.", seg_words)
    cues = captions.build_cues([segment], clip_start=10.0, clip_end=20.0)
    assert all(c.end <= 10.0 + 1e-6 for c in cues)


def test_ass_text_escaping_neutralises_override_codes_and_newlines():
    assert captions._escape_ass_text("{danger}") == "\\{danger\\}"
    assert captions._escape_ass_text("back\\slash") == "back\\\\slash"
    assert captions._escape_ass_text("line\nbreak") == "line break"


def test_format_timestamp_matches_ass_h_mm_ss_cc():
    assert captions._format_timestamp(0.0) == "0:00:00.00"
    assert captions._format_timestamp(65.5) == "0:01:05.50"
    assert captions._format_timestamp(3661.004) == "1:01:01.00"


def test_build_ass_document_contains_header_and_every_cue():
    cues = [
        captions.CaptionCue(0.0, 1.5, "Hello world"),
        captions.CaptionCue(2.0, 3.5, "{brace} test"),
    ]
    doc = captions.build_ass_document(cues, 1080, 1920)
    assert "PlayResX: 1080" in doc
    assert "PlayResY: 1920" in doc
    assert "[V4+ Styles]" in doc
    assert doc.count("Dialogue:") == 2
    assert "Hello world" in doc
    assert "\\{brace\\}" in doc  # escaped, not interpreted as an override


def test_build_ass_document_style_uses_the_configured_margins():
    cues = [captions.CaptionCue(0.0, 1.0, "x")]
    doc = captions.build_ass_document(
        cues, 1080, 1920, margin_bottom=300, margin_side=50,
    )
    style_line = next(l for l in doc.splitlines() if l.startswith("Style: Default"))
    fields = style_line.split(",")
    # MarginL, MarginR, MarginV are the last three before Encoding.
    assert fields[-4:-1] == ["50", "50", "300"]


def test_write_ass_creates_the_file(tmp_path):
    cues = [captions.CaptionCue(0.0, 1.0, "hi")]
    out = captions.write_ass(cues, tmp_path / "sub" / "clip.ass", 1080, 1920)
    assert out.exists()
    assert "Dialogue:" in out.read_text(encoding="utf-8")


def test_escape_filter_path_handles_colons_and_quotes():
    escaped = captions.escape_filter_path("/tmp/a:b's.ass")
    assert escaped.startswith("'") and escaped.endswith("'")
    assert "\\:" in escaped
    assert "\\'" in escaped
