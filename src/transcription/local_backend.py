"""Local Whisper backend via faster-whisper — the no-credential fallback.

This is why the project does not depend on any transcription vendor staying
free. No key, no quota, no network: a CTranslate2 Whisper model running on
the CI runner.

Cost of that independence is time. On a public repository's 4-vCPU runner the
``base`` model runs at roughly real time, so a 3-hour source is a long job —
but Actions minutes are unmetered on public repos, so a long job is free.
``small`` is noticeably better and roughly 2-3x slower; ``tiny`` is for tests.

Chunking is not needed here (there is no upload limit), but the same chunk
machinery is still used when it is active, so both backends go down one code
path and the timestamp-offset logic has one implementation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .types import Segment, TranscriptionError, Word

# Whisper's native input: 32-bit float PCM, mono, 16 kHz.
WHISPER_SAMPLE_RATE = 16000

DEFAULT_MODEL = "base"

# CPU-only int8 is the right default for a CI runner: no GPU, and int8 is
# several times faster than float32 with little accuracy cost at this size.
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"


class LocalWhisperTranscriber:
    """Transcribes with a locally-run Whisper model."""

    name = "local-whisper"

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        language: Optional[str] = None,
        vocabulary_prompt: Optional[str] = None,
        beam_size: int = 5,
        vad_filter: bool = True,
        word_timestamps: bool = True,
        model_factory=None,
        decoder=None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.vocabulary_prompt = vocabulary_prompt
        self.beam_size = beam_size
        # Voice-activity filtering drops silence before the model sees it,
        # which both speeds things up and suppresses the hallucinated text
        # Whisper tends to emit over long silences.
        self.vad_filter = vad_filter
        # ON by default, and not merely for captions: without it Whisper's
        # SEGMENT end times are wrong. Measured against audio with known
        # timing — an utterance ending at 3.68s was reported as ending at
        # 7.78s, stretched across the following silence. With word timing the
        # same segment came back as 1.62-3.74. Costs some speed.
        self.word_timestamps = word_timestamps
        self._model_factory = model_factory
        # Injectable for the same reason as model_factory: a test should not
        # need a real encoded audio file to exercise the segment handling.
        self._decoder = decoder
        self._model = None

    @property
    def max_bytes(self) -> Optional[int]:
        """No upload limit — nothing leaves the machine."""
        return None

    def _load(self):
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError(
                "faster-whisper is not installed, so the local transcription "
                "fallback is unavailable.\n"
                "  pip install faster-whisper\n"
                "Or set TRANSCRIPTION_API_KEY to use the Groq backend instead."
            ) from exc
        # First call downloads the model (~75 MB for `base`) and caches it.
        self._model = WhisperModel(
            self.model_size, device=self.device, compute_type=self.compute_type
        )
        return self._model

    def transcribe_file(self, audio_path: Path) -> Tuple[List[Segment], Optional[str]]:
        """Transcribe one file. Returns ``(segments, detected_language)``."""
        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise TranscriptionError("audio file does not exist: %s" % audio_path)

        model = self._load()
        # Decode with FFmpeg and hand the model a float array, rather than
        # letting faster-whisper open the file itself. Two reasons:
        #
        # 1. faster-whisper decodes via PyAV, and PyAV is unpinned in its
        #    requirements — so a fresh install picks up a PyAV whose API has
        #    moved and every transcription dies with
        #    `open() got an unexpected keyword argument 'metadata_errors'`.
        #    Observed on a clean install, 2026-10-10: faster-whisper 1.2.1 +
        #    av 19.0.1. Pinning PyAV instead would mean compiling it against
        #    FFmpeg headers, which has no wheel on every platform.
        # 2. FFmpeg is already a hard requirement of this project, and we
        #    already know the audio is 16 kHz mono — so this path has fewer
        #    moving parts than the one it replaces, not more.
        decode = self._decoder or self._decode
        samples = decode(audio_path)
        try:
            raw_segments, info = model.transcribe(
                samples,
                language=self.language,
                beam_size=self.beam_size,
                vad_filter=self.vad_filter,
                word_timestamps=self.word_timestamps,
                initial_prompt=self.vocabulary_prompt or None,
            )
            # faster-whisper returns a generator; transcription happens as it
            # is consumed, so the list() is where the work actually occurs.
            collected = list(raw_segments)
        except Exception as exc:
            raise TranscriptionError(
                "local Whisper failed on %s: %s" % (audio_path.name, exc)
            ) from exc

        segments: List[Segment] = []
        for item in collected:
            start = _attr(item, "start")
            end = _attr(item, "end")
            text = _attr(item, "text")
            if start is None or end is None:
                continue
            try:
                start = float(start)
                end = float(end)
            except (TypeError, ValueError):
                continue
            if end < start:
                continue
            segments.append(
                Segment(start, end, str(text or "").strip(), _words_of(item))
            )

        language = _attr(info, "language")
        return segments, (str(language) if language else None)


    @staticmethod
    def _decode(audio_path: Path):
        """Decode any audio file to a mono float32 array at 16 kHz via FFmpeg."""
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - ships with ctranslate2
            raise TranscriptionError(
                "numpy is required for local transcription: pip install numpy"
            ) from exc

        cmd = [
            "ffmpeg", "-nostdin", "-threads", "1", "-i", str(audio_path),
            "-f", "f32le", "-ac", "1", "-ar", str(WHISPER_SAMPLE_RATE),
            "-acodec", "pcm_f32le", "-loglevel", "error", "-",
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )
        except OSError as exc:
            raise TranscriptionError(
                "could not run FFmpeg to decode %s: %s" % (audio_path.name, exc)
            ) from exc
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", "replace").strip()[-300:]
            raise TranscriptionError(
                "FFmpeg could not decode %s: %s" % (audio_path.name, tail)
            )
        if not result.stdout:
            raise TranscriptionError(
                "FFmpeg decoded %s to zero samples" % audio_path.name
            )
        return np.frombuffer(result.stdout, dtype=np.float32)


def _words_of(item: Any) -> Optional[List[Word]]:
    """Extract per-word timings from a faster-whisper segment, if present."""
    raw = _attr(item, "words")
    if not raw:
        return None
    words: List[Word] = []
    for entry in raw:
        start = _attr(entry, "start")
        end = _attr(entry, "end")
        text = _attr(entry, "word")
        if text is None:
            text = _attr(entry, "text")
        if start is None or end is None:
            continue
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if end < start:
            continue
        words.append(Word(start, end, str(text or "").strip()))
    return words or None


def _attr(obj: Any, name: str):
    """Read an attribute or a dict key, so fakes need not mimic a namedtuple."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
