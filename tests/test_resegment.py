"""Rebuilding segment boundaries from word timings.

Whisper's own segment grouping varies with the audio encoding — measured: the
same speech and model gave 3 segments from a WAV and 1 fifteen-second segment
from the 32 kbit/s MP3 the pipeline produces, with identical word timings. So
grouping is decided here instead, deterministically.
"""

import pytest

from src.transcription.types import Segment, Word, resegment_from_words


def words(*specs):
    return [Word(s, e, t) for s, e, t in specs]


def one(segment_words, **kw):
    seg = Segment(segment_words[0].start, segment_words[-1].end,
                  " ".join(w.text for w in segment_words), segment_words)
    return resegment_from_words([seg], **kw)


def test_a_sentence_ending_closes_a_segment():
    out = one(words((0.0, 0.4, "Lights"), (0.4, 0.8, "out."),
                    (2.0, 2.4, "Box"), (2.4, 2.8, "box.")))
    assert [s.text for s in out] == ["Lights out.", "Box box."]


def test_question_and_exclamation_also_close():
    out = one(words((0.0, 0.3, "What?"), (0.5, 0.9, "Go!")))
    assert [s.text for s in out] == ["What?", "Go!"]


def test_a_long_pause_closes_a_segment_even_mid_sentence():
    """Natural phrase boundary; also how team radio actually sounds."""
    out = one(
        words((0.0, 0.4, "and"), (0.4, 0.8, "then"), (3.0, 3.4, "suddenly")),
        pause_threshold=0.8,
    )
    assert [s.text for s in out] == ["and then", "suddenly"]


def test_a_short_gap_does_not_close_a_segment():
    out = one(
        words((0.0, 0.4, "and"), (0.5, 0.9, "then"), (1.0, 1.4, "suddenly")),
        pause_threshold=0.8,
    )
    assert len(out) == 1


def test_max_seconds_closes_a_runaway_segment():
    long_words = words(*[(float(i), float(i) + 0.5, "w%d" % i) for i in range(20)])
    out = one(long_words, max_seconds=5.0, pause_threshold=99.0)
    assert len(out) > 1
    for segment in out[:-1]:
        assert segment.end - segment.start <= 5.6


def test_max_chars_closes_a_wide_segment():
    wide = words(*[(float(i) * 0.1, float(i) * 0.1 + 0.05, "word" * 5)
                   for i in range(20)])
    out = one(wide, max_chars=60, max_seconds=99.0, pause_threshold=99.0)
    assert len(out) > 1
    for segment in out[:-1]:
        assert len(segment.text) <= 120


def test_boundaries_come_from_the_words_not_the_original_segment():
    """The whole point: the incoming segment's own span is 0-15s and wrong."""
    segment_words = words((1.62, 3.20, "Lights out."), (7.68, 9.84, "Overtake."))
    wrong = Segment(0.0, 15.0, "Lights out. Overtake.", segment_words)
    out = resegment_from_words([wrong])
    assert out[0].start == pytest.approx(1.62)
    assert out[0].end == pytest.approx(3.20)
    assert out[1].start == pytest.approx(7.68)
    assert out[1].end == pytest.approx(9.84)


def test_every_word_survives_exactly_once():
    specs = [(float(i) * 0.5, float(i) * 0.5 + 0.4, "w%d" % i) for i in range(30)]
    out = one(words(*specs), max_seconds=2.0)
    produced = [w.text for s in out for w in (s.words or [])]
    assert produced == [t for _s, _e, t in specs]


def test_word_timings_are_carried_into_the_new_segments():
    out = one(words((0.0, 0.4, "Lights"), (0.4, 0.8, "out.")))
    assert out[0].words is not None
    assert [w.text for w in out[0].words] == ["Lights", "out."]


def test_a_segment_without_word_timings_passes_through_untouched():
    """A backend that cannot supply words must still work."""
    plain = Segment(1.0, 9.0, "no word timings here")
    assert resegment_from_words([plain]) == [plain]


def test_a_mixture_is_handled():
    plain = Segment(20.0, 25.0, "plain")
    timed = Segment(0.0, 1.0, "a. b.", words((0.0, 0.4, "a."), (0.5, 1.0, "b.")))
    out = resegment_from_words([timed, plain])
    assert [s.text for s in out] == ["a.", "b.", "plain"]


def test_empty_input_is_empty_output():
    assert resegment_from_words([]) == []


def test_segments_never_end_before_they_start():
    out = one(words(*[(float(i) * 0.3, float(i) * 0.3 + 0.2, "w%d" % i)
                      for i in range(12)]), max_seconds=1.0)
    for segment in out:
        assert segment.end >= segment.start
