"""The analysis pass end to end, with fakes: candidates -> ranked clips in the DB."""

import pytest

from src.analysis import runner
from src.analysis.types import AnalysisError, AnalysisQuotaExceeded, ClipCandidate, SubScores
from src.database import db
from src.transcription.types import Segment, Transcript
from src.utils.config import Config
from src.utils.logging import get_logger


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def make_config(**analysis_overrides):
    analysis = {"backend": "heuristic", "max_videos_per_run": 3, "max_retries": 3,
               "overlap_threshold": 0.5, "similarity_threshold": 0.6}
    analysis.update(analysis_overrides)
    return Config(
        database_path=":memory:",
        monitoring=None,
        raw={
            "clip": {"min_duration": 5, "max_duration": 60, "target_duration": 20,
                     "candidates_per_video": 8},
            "analysis": analysis,
        },
    )


def seed_transcribed_video(conn, tmp_path, video_id="VID1", segments=None, title="A video"):
    from src.utils.config import Source

    source = Source("src", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    source_id = db.sync_sources(conn, [source])["src"]
    _, row_id = db.record_discovered_video(
        conn, source_id, video_id, title, "https://x",
        "2026-10-10T00:00:00+00:00", status=db.VideoStatus.TRANSCRIBED,
    )
    segments = segments or [
        Segment(0, 10, "Box box box, incredible overtake into turn one!"),
        Segment(10, 25, "He is through, massive move for the lead."),
    ]
    transcript = Transcript(segments=segments, backend="test", source_duration=30.0)
    path = tmp_path / ("%s.json" % video_id)
    transcript.write(path)
    db.record_transcript(conn, row_id, str(path), "test", "model", "en", len(segments),
                         sum(s.duration for s in segments), 1)
    return row_id


class FakeSelector:
    """Stands in for GeminiClipSelector with a scripted outcome."""

    def __init__(self, outcome=None, **kw):
        self.outcome = outcome
        self.kw = kw

    def score_windows(self, windows, source_title="", vocabulary_hint=""):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.outcome is not None:
            return self.outcome
        return [
            ClipCandidate(start=w.start, end=w.end,
                         sub_scores=SubScores(70, 70, 70, 70, 70),
                         reason="r", hook="h", text=w.text, method="llm")
            for w in windows
        ]


def selector_factory(outcome=None):
    def factory(**kw):
        return FakeSelector(outcome=outcome, **kw)
    return factory


# --- heuristic path (no key needed) -----------------------------------------


def test_a_transcribed_video_is_analyzed_with_the_heuristic_backend(conn, tmp_path):
    row_id = seed_transcribed_video(conn, tmp_path)
    result = runner.run_analysis(conn, make_config(), log=get_logger("t"))

    assert result.analyzed == 1
    row = db.get_video(conn, "VID1")
    assert row["status"] == "ANALYZED"
    assert row["candidates_found"] >= 1
    clips = db.clips_for_video(conn, row_id)
    assert len(clips) >= 1
    assert clips[0]["scoring_method"] == "heuristic"


def test_a_second_pass_does_nothing(conn, tmp_path):
    seed_transcribed_video(conn, tmp_path)
    runner.run_analysis(conn, make_config(), log=get_logger("t"))
    second = runner.run_analysis(conn, make_config(), log=get_logger("t"))
    assert second.analyzed == 0
    assert second.outcomes == []


def test_a_video_with_no_viable_windows_is_skipped_terminally(conn, tmp_path):
    seed_transcribed_video(conn, tmp_path, segments=[Segment(0, 2, "too short")])
    runner.run_analysis(conn, make_config(), log=get_logger("t"))
    row = db.get_video(conn, "VID1")
    assert row["status"] == "SKIPPED"
    assert "no candidate window" in row["status_detail"]


def test_a_missing_transcript_file_fails_retryably(conn, tmp_path):
    row_id = seed_transcribed_video(conn, tmp_path)
    db.record_transcript(conn, row_id, str(tmp_path / "nonexistent.json"),
                         "test", "model", "en", 1, 10.0, 1)
    db.set_video_status(conn, row_id, db.VideoStatus.TRANSCRIBED)
    result = runner.run_analysis(conn, make_config(), log=get_logger("t"))
    assert len(result.failed) == 1
    row = db.get_video(conn, "VID1")
    assert row["status"] == "FAILED"
    assert row["retry_count"] == 1


# --- llm path with fakes -----------------------------------------------------


def test_llm_backend_produces_llm_tagged_clips(conn, tmp_path):
    seed_transcribed_video(conn, tmp_path)
    config = make_config(backend="llm")
    result = runner.run_analysis(
        conn, config, log=get_logger("t"), api_key="KEY",
        selector_factory=selector_factory(),
    )
    assert result.analyzed == 1
    clips = db.clips_for_video(conn, 1)
    assert all(c["scoring_method"] == "llm" for c in clips)


def test_llm_backend_without_a_key_fails_rather_than_silently_degrading(conn, tmp_path):
    """An EXPLICIT 'llm' choice must error, not silently fall back — only
    'auto' falls back."""
    seed_transcribed_video(conn, tmp_path)
    config = make_config(backend="llm")
    result = runner.run_analysis(conn, config, log=get_logger("t"), api_key=None)
    assert len(result.failed) == 1
    assert "LLM_API_KEY" in result.outcomes[0].reason


def test_auto_uses_the_heuristic_with_no_key(conn, tmp_path):
    seed_transcribed_video(conn, tmp_path)
    config = make_config(backend="auto")
    result = runner.run_analysis(conn, config, log=get_logger("t"), api_key=None)
    assert result.analyzed == 1
    clips = db.clips_for_video(conn, 1)
    assert clips[0]["scoring_method"] == "heuristic"


def test_a_spent_quota_defers_without_marking_a_failure(conn, tmp_path):
    seed_transcribed_video(conn, tmp_path)
    config = make_config(backend="llm")
    outcome = AnalysisQuotaExceeded("spent")
    result = runner.run_analysis(
        conn, config, log=get_logger("t"), api_key="KEY",
        selector_factory=selector_factory(outcome),
    )
    row = db.get_video(conn, "VID1")
    assert row["status"] == "TRANSCRIBED"     # unchanged, retried next run
    assert row["retry_count"] == 0
    assert result.failed == []
    assert result.outcomes[0].status == "DEFERRED"


def test_an_llm_failure_falls_back_to_heuristic_by_default(conn, tmp_path):
    seed_transcribed_video(conn, tmp_path)
    config = make_config(backend="llm", allow_heuristic_fallback=True)
    outcome = AnalysisError("boom")
    result = runner.run_analysis(
        conn, config, log=get_logger("t"), api_key="KEY",
        selector_factory=selector_factory(outcome),
    )
    assert result.analyzed == 1
    clips = db.clips_for_video(conn, 1)
    assert clips[0]["scoring_method"] == "heuristic"


def test_fallback_can_be_disabled(conn, tmp_path):
    seed_transcribed_video(conn, tmp_path)
    config = make_config(backend="llm", allow_heuristic_fallback=False)
    outcome = AnalysisError("boom")
    result = runner.run_analysis(
        conn, config, log=get_logger("t"), api_key="KEY",
        selector_factory=selector_factory(outcome),
    )
    assert len(result.failed) == 1
    row = db.get_video(conn, "VID1")
    assert row["status"] == "FAILED"


# --- dedup wired into the runner --------------------------------------------


def test_candidates_capped_at_candidates_per_video(conn, tmp_path):
    segments = [Segment(i * 30, i * 30 + 10, "Box box box overtake %d!" % i)
               for i in range(20)]
    seed_transcribed_video(conn, tmp_path, segments=segments)
    config = make_config()
    config.raw["clip"]["candidates_per_video"] = 3
    result = runner.run_analysis(conn, config, log=get_logger("t"))
    assert result.outcomes[0].candidates <= 3


def test_overlapping_windows_are_collapsed_before_storage(conn, tmp_path):
    """Heuristic scoring on overlapping, near-identical windows must not
    store near-duplicate clips for the same moment."""
    segments = [Segment(0, 10, "Box box box!"), Segment(10, 15, "incredible!")]
    seed_transcribed_video(conn, tmp_path, segments=segments)
    result = runner.run_analysis(conn, make_config(), log=get_logger("t"))
    clips = db.clips_for_video(conn, 1)
    # Windows anchored at segment 0 and segment 1 overlap heavily; only the
    # non-overlapping subset should survive.
    starts = [c["start_ms"] for c in clips]
    assert len(starts) == len(set(starts))


def test_a_candidate_too_similar_to_an_already_queued_clip_is_dropped(conn, tmp_path):
    row_id = seed_transcribed_video(conn, tmp_path)
    # Pre-seed a QUEUED clip with text matching what the heuristic scorer
    # will produce for this video's own segments.
    conn.execute(
        "INSERT INTO clips (source_video_id, start_ms, end_ms, status, "
        "transcript_text, created_at) VALUES (?, 999000, 1029000, ?, ?, ?)",
        (row_id, db.ClipStatus.QUEUED.value,
         "Box box box, incredible overtake into turn one! He is through, "
         "massive move for the lead.", db.utcnow()),
    )
    conn.commit()
    result = runner.run_analysis(conn, make_config(), log=get_logger("t"))
    # The one viable window's text is near-identical to the queued clip, so
    # it should be dropped as a duplicate -> zero stored candidates -> SKIPPED.
    row = db.get_video(conn, "VID1")
    assert row["status"] == "SKIPPED"
    assert "near-duplicate" in row["status_detail"]
