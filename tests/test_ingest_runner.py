"""The ingest pass: download then transcribe, in one run.

They are welded together because the media file only exists inside a single
workflow run — too large to commit, and the runner is destroyed afterwards.
"""

import pytest

from src.database import db
from src.download import downloader
from src.transcription import runner as ingest
from src.transcription.types import (
    Segment,
    Transcript,
    TranscriptionError,
    TranscriptionQuotaExceeded,
    Word,
)
from src.utils.config import Config, MonitoringConfig, Source
from src.utils.logging import get_logger
from tests.fakes_ytdlp import REAL_INFO, factory
from tests.test_youtube_api import FakeResponse, FakeSession


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def ffmpeg_present(monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)


def make_config(**transcription_overrides):
    transcription = {"backend": "local", "local_model": "tiny",
                     "max_videos_per_run": 2}
    transcription.update(transcription_overrides)
    return Config(
        database_path=":memory:",
        monitoring=MonitoringConfig(max_retries=1, backoff_base_seconds=0),
        raw={
            "processing": {"max_source_duration_minutes": 180,
                           "max_download_size_mb": 2000},
            "download": {"require_creative_commons": True,
                         "min_source_duration_seconds": 120,
                         "player_clients": ["default"],
                         "max_videos_per_run": 5, "max_retries": 3},
            "transcription": transcription,
        },
    )


def seed(conn, video_id="VID1"):
    source = Source("src", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    source_id = db.sync_sources(conn, [source])["src"]
    _, row_id = db.record_discovered_video(
        conn, source_id, video_id, "A video",
        "https://www.youtube.com/watch?v=%s" % video_id,
        "2026-10-10T00:00:00+00:00",
    )
    return row_id


def details(video_id="VID1"):
    return FakeResponse(200, {"items": [{
        "id": video_id,
        "snippet": {"title": "t", "liveBroadcastContent": "none"},
        "contentDetails": {"duration": "PT10M"},
        "status": {"license": "creativeCommon", "privacyStatus": "public"},
    }]})


def writes_media(dest, info):
    from pathlib import Path
    p = Path(dest) / ("%s.mp4" % info["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * 4096)
    return p


def good_transcript(*_a, **_kw):
    return Transcript(
        segments=[Segment(1.0, 3.0, "Lights out.",
                          [Word(1.0, 1.5, "Lights"), Word(1.5, 3.0, "out.")])],
        language="en", backend="local-whisper", model="tiny",
        source_duration=600.0, chunk_count=1,
    )


def run(conn, tmp_path, session, transcribe_fn=good_transcript, config=None):
    return ingest.run_ingest(
        conn, config or make_config(), dest_dir=tmp_path, log=get_logger("test"),
        transcript_dir=tmp_path / "transcripts",
        youtube_api_key="KEY", transcription_api_key=None, session=session,
        ydl_factory=factory({"default": REAL_INFO}, write_file=writes_media),
        transcribe_fn=transcribe_fn,
    )


# --- the happy path --------------------------------------------------------


def test_a_video_is_downloaded_then_transcribed_in_one_pass(conn, tmp_path):
    seed(conn)
    result = run(conn, tmp_path, FakeSession([details()]))

    assert result.downloaded == 1
    assert result.transcribed == 1
    row = db.get_video(conn, "VID1")
    assert row["status"] == "TRANSCRIBED"
    assert row["transcript_segments"] == 1
    assert row["transcript_backend"] == "local-whisper"
    assert row["transcript_language"] == "en"
    assert row["transcribed_at"] is not None


def test_the_transcript_is_written_to_disk_and_is_readable(conn, tmp_path):
    seed(conn)
    run(conn, tmp_path, FakeSession([details()]))
    path = tmp_path / "transcripts" / "VID1.json"
    assert path.exists()
    loaded = Transcript.read(path)
    assert loaded.segments[0].text == "Lights out."
    assert loaded.has_word_timings is True


def test_a_second_pass_does_nothing(conn, tmp_path):
    """Idempotency across the whole ingest pass."""
    seed(conn)
    run(conn, tmp_path, FakeSession([details()]))
    second = run(conn, tmp_path, FakeSession([]))
    assert second.downloaded == 0
    assert second.transcribed == 0


# --- the gate still governs -----------------------------------------------


def test_a_video_the_gate_skips_is_never_transcribed(conn, tmp_path):
    seed(conn)
    not_cc = FakeResponse(200, {"items": [{
        "id": "VID1", "snippet": {"title": "t", "liveBroadcastContent": "none"},
        "contentDetails": {"duration": "PT10M"},
        "status": {"license": "youtube"},
    }]})

    def must_not_run(*a, **kw):
        raise AssertionError("transcription must not be reached")

    result = run(conn, tmp_path, FakeSession([not_cc]), transcribe_fn=must_not_run)
    assert result.transcribed == 0
    assert db.get_video(conn, "VID1")["status"] == "SKIPPED"


# --- transcription failures ------------------------------------------------


def test_a_transcription_failure_is_FAILED_and_retryable(conn, tmp_path):
    seed(conn)

    def boom(*a, **kw):
        raise TranscriptionError("model exploded")

    result = run(conn, tmp_path, FakeSession([details()]), transcribe_fn=boom)
    row = db.get_video(conn, "VID1")
    assert row["status"] == "FAILED"
    assert row["retry_count"] == 1
    assert "model exploded" in row["status_detail"]
    assert len(result.failed) == 1


def test_a_spent_allowance_defers_without_marking_a_failure(conn, tmp_path):
    """Nothing is wrong with the video or the code, so it must not count as a
    failure or burn a retry."""
    seed(conn)

    def spent(*a, **kw):
        raise TranscriptionQuotaExceeded("daily allowance spent")

    result = run(conn, tmp_path, FakeSession([details()]), transcribe_fn=spent)
    row = db.get_video(conn, "VID1")
    assert row["status"] == "DOWNLOADED"       # picked up next run
    assert row["retry_count"] == 0
    assert result.failed == []
    assert result.outcomes[-1].status == "DEFERRED"


def test_a_silent_video_is_skipped_terminally(conn, tmp_path):
    """Re-running yields the same silence, so retrying is pointless."""
    seed(conn)

    def silent(*a, **kw):
        return Transcript(segments=[], backend="local-whisper",
                          source_duration=600.0)

    run(conn, tmp_path, FakeSession([details()]), transcribe_fn=silent)
    row = db.get_video(conn, "VID1")
    assert row["status"] == "SKIPPED"
    assert "no speech" in row["status_detail"]


def test_a_whitespace_only_transcript_counts_as_silent(conn, tmp_path):
    seed(conn)

    def blank(*a, **kw):
        return Transcript(segments=[Segment(0, 1, "   ")],
                          backend="local-whisper", source_duration=600.0)

    run(conn, tmp_path, FakeSession([details()]), transcribe_fn=blank)
    assert db.get_video(conn, "VID1")["status"] == "SKIPPED"


# --- transcript pruning ----------------------------------------------------


def test_pruning_removes_transcripts_only_for_terminal_videos(conn, tmp_path):
    row_id = seed(conn)
    run(conn, tmp_path, FakeSession([details()]))
    path = tmp_path / "transcripts" / "VID1.json"
    assert path.exists()

    # TRANSCRIBED is not terminal — the clip has not been made yet.
    assert ingest.prune_transcripts(conn, tmp_path / "transcripts") == []
    assert path.exists()

    db.set_video_status(conn, row_id, db.VideoStatus.PUBLISHED)
    assert ingest.prune_transcripts(conn, tmp_path / "transcripts") == ["VID1"]
    assert not path.exists()
    assert db.get_video(conn, "VID1")["transcript_path"] is None


def test_pruning_a_missing_file_does_not_raise(conn, tmp_path):
    row_id = seed(conn)
    run(conn, tmp_path, FakeSession([details()]))
    (tmp_path / "transcripts" / "VID1.json").unlink()
    db.set_video_status(conn, row_id, db.VideoStatus.SKIPPED)
    assert ingest.prune_transcripts(conn, tmp_path / "transcripts") == ["VID1"]


def test_pending_transcription_lists_downloaded_videos(conn, tmp_path):
    row_id = seed(conn)
    assert db.videos_pending_transcription(conn) == []
    db.record_download(conn, row_id, str(tmp_path / "x.mp4"), 100)
    assert len(db.videos_pending_transcription(conn)) == 1
    db.set_video_status(conn, row_id, db.VideoStatus.TRANSCRIBED)
    assert db.videos_pending_transcription(conn) == []
