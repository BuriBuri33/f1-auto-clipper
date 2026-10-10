"""Backend selection, the fallback rule, and the end-to-end transcribe().

The interesting behaviour here is the fallback policy: a Groq failure falls
back to local Whisper, but a SPENT QUOTA does not. Quietly substituting an
hour of CPU for an API whose allowance ran out hides the problem from the one
person who can fix it.
"""

import pytest
import requests

from src.transcription import audio as audio_module
from src.transcription import transcriber as tr
from src.transcription.groq_backend import GroqTranscriber
from src.transcription.local_backend import LocalWhisperTranscriber
from src.transcription.types import (
    Segment,
    TranscriptionError,
    TranscriptionQuotaExceeded,
)


# --- backend selection -----------------------------------------------------


def test_auto_picks_groq_when_a_key_is_present():
    assert isinstance(tr.build_backend("auto", api_key="KEY"), GroqTranscriber)


def test_auto_picks_local_when_no_key_is_present(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_API_KEY", raising=False)
    assert isinstance(tr.build_backend("auto", api_key=None), LocalWhisperTranscriber)


def test_auto_reads_the_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", "FROM-ENV")
    backend = tr.build_backend("auto")
    assert isinstance(backend, GroqTranscriber)
    assert backend.api_key == "FROM-ENV"


def test_groq_without_a_key_is_an_actionable_error(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_API_KEY", raising=False)
    with pytest.raises(TranscriptionError, match="console.groq.com/keys"):
        tr.build_backend("groq", api_key=None)


def test_local_ignores_a_key_entirely():
    assert isinstance(tr.build_backend("local", api_key="KEY"), LocalWhisperTranscriber)


def test_an_unknown_backend_is_refused():
    with pytest.raises(TranscriptionError, match="not implemented"):
        tr.build_backend("deepgram")


def test_the_local_backend_declares_no_upload_limit():
    """Which is why chunking is skipped for it."""
    assert tr.build_backend("local").max_bytes is None
    assert tr.build_backend("auto", api_key="KEY").max_bytes == 25 * 1024 * 1024


# --- end to end, with the audio layer faked --------------------------------


@pytest.fixture()
def media(tmp_path):
    p = tmp_path / "video.mp4"
    p.write_bytes(b"\0" * 1024)
    return p


@pytest.fixture()
def fake_audio(monkeypatch, tmp_path):
    """Replace ffmpeg work with deterministic stand-ins."""
    def extract(media_path, out_dir, bitrate_kbps=32, sample_rate=16000):
        p = tmp_path / "audio.mp3"
        p.write_bytes(b"\0" * 2048)
        return p

    def prepare(media_path, out_dir, max_bytes, duration=None,
                bitrate_kbps=32, overlap=4.0):
        path = extract(media_path, out_dir)
        total = duration or 600.0
        return [audio_module.AudioChunk(path, 0, 0.0, total, 0.0, total)]

    monkeypatch.setattr(audio_module, "probe_duration", lambda p: 600.0)
    monkeypatch.setattr(audio_module, "extract_audio", extract)
    monkeypatch.setattr(audio_module, "prepare_chunks", prepare)


class FakeGroqSession:
    """Returns one scripted verbose_json body, or raises."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def post(self, *a, **kw):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome

        class R:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {
                    "language": "en",
                    "duration": 600.0,
                    "segments": [{"start": 1.0, "end": 3.0, "text": "lights out"}],
                }

        return R()


def test_transcribe_produces_a_transcript_in_the_spec_shape(media, fake_audio):
    transcript = tr.transcribe(
        media, backend="groq", api_key="KEY", session=FakeGroqSession(None)
    )
    assert transcript.backend == "groq"
    assert transcript.language == "en"
    assert transcript.source_duration == 600.0
    assert [s.text for s in transcript.segments] == ["lights out"]
    assert transcript.as_dict()["segments"][0]["start"] == 1.0


def test_the_local_backend_runs_through_the_same_path(media, fake_audio, monkeypatch):
    class FakeModel:
        def transcribe(self, path, language=None, beam_size=5, vad_filter=True,
                      word_timestamps=True):
            info = {"language": "it"}
            return iter([{"start": 2.0, "end": 4.0, "text": " forza "}]), info

    monkeypatch.setattr(
        tr, "build_backend",
        lambda *a, **kw: LocalWhisperTranscriber(
            model_size="tiny", model_factory=lambda *a, **kw: FakeModel(),
            decoder=lambda _p: [0.0],
        ),
    )
    transcript = tr.transcribe(media, backend="local")
    assert transcript.backend == "local-whisper"
    assert transcript.language == "it"
    assert [s.text for s in transcript.segments] == ["forza"]


def test_a_groq_failure_falls_back_to_local_whisper(media, fake_audio, monkeypatch):
    class FakeModel:
        def transcribe(self, path, language=None, beam_size=5, vad_filter=True,
                      word_timestamps=True):
            return iter([{"start": 0.0, "end": 1.0, "text": "from local"}]), {"language": "en"}

    real_build = tr.build_backend

    def build(backend="auto", *a, **kw):
        if backend == "local":
            return LocalWhisperTranscriber(
                model_size="tiny", model_factory=lambda *a, **kw: FakeModel(),
                decoder=lambda _p: [0.0]
            )
        return real_build(backend, *a, **kw)

    monkeypatch.setattr(tr, "build_backend", build)
    transcript = tr.transcribe(
        media, backend="auto", api_key="KEY",
        session=FakeGroqSession(requests.ConnectionError("groq unreachable")),
        sleep=lambda _: None,
    )
    assert transcript.backend == "local-whisper"
    assert [s.text for s in transcript.segments] == ["from local"]


def test_an_unexpected_error_type_also_falls_back(media, fake_audio, monkeypatch):
    """An unattended pipeline must not die because a provider raised something
    the backend did not anticipate."""
    class FakeModel:
        def transcribe(self, path, language=None, beam_size=5, vad_filter=True,
                      word_timestamps=True):
            return iter([{"start": 0.0, "end": 1.0, "text": "rescued"}]), {"language": "en"}

    real_build = tr.build_backend

    def build(backend="auto", *a, **kw):
        if backend == "local":
            return LocalWhisperTranscriber(
                model_size="tiny", model_factory=lambda *a, **kw: FakeModel(),
                decoder=lambda _p: [0.0]
            )
        return real_build(backend, *a, **kw)

    monkeypatch.setattr(tr, "build_backend", build)
    transcript = tr.transcribe(
        media, backend="auto", api_key="KEY",
        session=FakeGroqSession(ValueError("something unforeseen")),
        sleep=lambda _: None,
    )
    assert [s.text for s in transcript.segments] == ["rescued"]


def test_retry_backoff_is_injectable_so_tests_do_not_wait():
    """Mirrors the detector and downloader layers, which already do this."""
    backend = tr.build_backend("groq", api_key="KEY", sleep=lambda _: None)
    assert backend.sleep is not None
    assert backend.sleep(99) is None       # returns instantly


def test_a_spent_quota_does_NOT_fall_back(media, fake_audio, monkeypatch):
    """The important policy: an hour of silent CPU would hide the problem."""
    class Spent:
        def post(self, *a, **kw):
            class R:
                status_code = 429
                headers = {}

                @staticmethod
                def json():
                    return {"error": {"message": "audio seconds per day exceeded"}}

            return R()

    called = {"local": False}

    def build(backend="auto", *a, **kw):
        if backend == "local":
            called["local"] = True
        return tr.GroqTranscriber(api_key="KEY", session=Spent(), sleep=lambda _: None)

    monkeypatch.setattr(tr, "build_backend", build)
    with pytest.raises(TranscriptionQuotaExceeded):
        tr.transcribe(media, backend="auto", api_key="KEY", session=Spent())
    assert called["local"] is False


def test_fallback_can_be_switched_off(media, fake_audio):
    with pytest.raises(TranscriptionError):
        tr.transcribe(
            media, backend="auto", api_key="KEY",
            session=FakeGroqSession(requests.ConnectionError("boom")),
            allow_local_fallback=False,
            sleep=lambda _: None,
        )


def test_a_missing_media_file_is_refused_before_any_work(tmp_path):
    with pytest.raises(TranscriptionError, match="does not exist"):
        tr.transcribe(tmp_path / "nope.mp4", backend="local")


def test_the_local_backend_is_given_no_size_limit(media, monkeypatch, tmp_path):
    """No upload limit means one chunk and no splitting, which costs time."""
    monkeypatch.setattr(audio_module, "probe_duration", lambda p: 10800.0)
    seen = {}

    def prepare(media_path, out_dir, max_bytes, duration=None,
                bitrate_kbps=32, overlap=4.0):
        seen["max_bytes"] = max_bytes
        return [audio_module.AudioChunk(
            _write(tmp_path / "audio.mp3", 1024), 0, 0.0, 10800.0, 0.0, 10800.0)]

    monkeypatch.setattr(audio_module, "prepare_chunks", prepare)

    class FakeModel:
        def transcribe(self, path, language=None, beam_size=5, vad_filter=True,
                      word_timestamps=True):
            return iter([{"start": 0.0, "end": 1.0, "text": "x"}]), {"language": "en"}

    monkeypatch.setattr(
        tr, "build_backend",
        lambda *a, **kw: LocalWhisperTranscriber(
            model_size="tiny", model_factory=lambda *a, **kw: FakeModel(),
            decoder=lambda _p: [0.0],
        ),
    )
    tr.transcribe(media, backend="local")
    assert seen["max_bytes"] is None


def _write(path, size):
    path.write_bytes(b"\0" * size)
    return path
