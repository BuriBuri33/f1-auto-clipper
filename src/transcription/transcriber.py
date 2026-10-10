"""The transcription interface the specification asks for (section 9).

    transcribe(media_path) -> Transcript

One function, one return type, backends behind it. Everything downstream —
clip selection, captions — reads only ``Transcript``, which is what makes the
layer replaceable rather than nominally replaceable.

Backend selection mirrors the detector layer's ``auto``:

    auto    Groq when TRANSCRIPTION_API_KEY is set, local Whisper otherwise,
            and local Whisper as a fallback if a Groq attempt fails for a
            reason that is not a spent quota
    groq    Groq only
    local   local Whisper only

The fallback deliberately excludes a spent quota: falling back to a local
model that takes an hour, every run, while the operator believes the API is
working, hides the problem instead of surfacing it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional, Sequence

from . import audio as audio_module
from .groq_backend import DEFAULT_MODEL as GROQ_DEFAULT_MODEL
from .groq_backend import GroqTranscriber, parse_verbose_json
from .local_backend import DEFAULT_MODEL as LOCAL_DEFAULT_MODEL
from .local_backend import LocalWhisperTranscriber
from .types import (
    Segment,
    Transcript,
    TranscriptionError,
    TranscriptionQuotaExceeded,
    normalise,
    resegment_from_words,
)

VALID_BACKENDS = ("auto", "groq", "local")


def build_backend(
    backend: str = "auto",
    api_key: Optional[str] = None,
    groq_model: Optional[str] = None,
    local_model: Optional[str] = None,
    language: Optional[str] = None,
    session=None,
    sleep=time.sleep,
    word_timestamps: bool = True,
):
    """Construct the backend named by ``backend``, or None when unavailable."""
    if backend not in VALID_BACKENDS:
        raise TranscriptionError(
            "transcription.backend %r is not implemented. Valid: %s"
            % (backend, ", ".join(VALID_BACKENDS))
        )
    if api_key is None:
        api_key = os.environ.get("TRANSCRIPTION_API_KEY") or None

    if backend == "groq" or (backend == "auto" and api_key):
        if not api_key:
            raise TranscriptionError(
                "transcription.backend is 'groq' but TRANSCRIPTION_API_KEY is "
                "not set. Get a free key at https://console.groq.com/keys, or "
                "set the backend to 'local'."
            )
        return GroqTranscriber(
            api_key=api_key,
            model=groq_model or GROQ_DEFAULT_MODEL,
            language=language,
            session=session,
            sleep=sleep,
        )
    return LocalWhisperTranscriber(
        model_size=local_model or LOCAL_DEFAULT_MODEL,
        language=language,
        word_timestamps=word_timestamps,
    )


def _segments_from(backend, chunk_path: Path):
    """Call whichever backend and normalise its return into segments + language."""
    if isinstance(backend, GroqTranscriber):
        payload = backend.transcribe_file(chunk_path)
        return parse_verbose_json(payload), payload.get("language")
    segments, language = backend.transcribe_file(chunk_path)
    return segments, language


def transcribe(
    media_path: Path,
    work_dir: Optional[Path] = None,
    backend: str = "auto",
    api_key: Optional[str] = None,
    groq_model: Optional[str] = None,
    local_model: Optional[str] = None,
    language: Optional[str] = None,
    bitrate_kbps: int = audio_module.DEFAULT_BITRATE_KBPS,
    overlap_seconds: float = audio_module.DEFAULT_OVERLAP_SECONDS,
    allow_local_fallback: bool = True,
    word_timestamps: bool = True,
    resegment: bool = True,
    max_segment_seconds: float = 8.0,
    max_segment_chars: int = 180,
    pause_threshold: float = 0.8,
    session=None,
    sleep=time.sleep,
    log=None,
) -> Transcript:
    """Transcribe a media file into a ``Transcript``.

    Steps: extract 16 kHz mono audio, split it to fit the backend's upload
    limit, transcribe each chunk, shift the timestamps back onto the source
    timeline, merge, and clean up.
    """
    media_path = Path(media_path)
    if not media_path.exists():
        raise TranscriptionError("media file does not exist: %s" % media_path)

    work_dir = Path(work_dir) if work_dir else media_path.parent
    chosen = build_backend(
        backend, api_key, groq_model, local_model, language, session, sleep,
        word_timestamps=word_timestamps,
    )

    duration = audio_module.probe_duration(media_path)
    chunks = audio_module.prepare_chunks(
        media_path,
        work_dir,
        max_bytes=getattr(chosen, "max_bytes", None),
        duration=duration,
        bitrate_kbps=bitrate_kbps,
        overlap=overlap_seconds,
    )

    if log:
        log.info(
            "transcribing",
            fields={"backend": chosen.name, "chunks": len(chunks),
                    "duration_s": round(duration, 1)},
        )

    per_chunk: List[Sequence[Segment]] = []
    detected_language = language
    try:
        for chunk in chunks:
            segments, chunk_language = _segments_from(chosen, chunk.path)
            per_chunk.append(segments)
            if detected_language is None and chunk_language:
                detected_language = chunk_language
    except TranscriptionQuotaExceeded:
        # Never silently substitute an hour of local compute for an API whose
        # allowance has run out — the operator needs to see it.
        raise
    except Exception as exc:
        # Deliberately broad. The point of a fallback is "the primary did not
        # work, for whatever reason" — and in an unattended pipeline, an
        # unexpected exception type crashing the pass is strictly worse than
        # transcribing locally and carrying on. The cost of breadth is that a
        # genuine bug in this code could hide behind the fallback, so the
        # exception TYPE is logged, not just its message: an unexpected type
        # in the logs is the signal that something other than the provider is
        # wrong.
        if not (backend == "auto" and allow_local_fallback
                and isinstance(chosen, GroqTranscriber)):
            raise
        if log:
            log.warning(
                "groq transcription failed, falling back to local Whisper",
                fields={"error_type": type(exc).__name__, "error": exc},
            )
        return transcribe(
            media_path, work_dir=work_dir, backend="local",
            local_model=local_model, language=language,
            bitrate_kbps=bitrate_kbps, overlap_seconds=overlap_seconds,
            allow_local_fallback=False, word_timestamps=word_timestamps,
            resegment=resegment, max_segment_seconds=max_segment_seconds,
            max_segment_chars=max_segment_chars, pause_threshold=pause_threshold,
            sleep=sleep, log=log,
        )

    merged = audio_module.merge_chunk_segments(chunks, per_chunk)
    segments = normalise(merged, source_duration=duration)

    # Rebuild segment boundaries from word timings. The backend's own grouping
    # varies with the audio encoding (measured: 3 segments from a WAV vs 1
    # from the 32 kbit/s MP3 this pipeline produces, same speech, same model),
    # while the word timings are stable. See resegment_from_words.
    if resegment and any(s.words for s in segments):
        segments = resegment_from_words(
            segments,
            max_seconds=max_segment_seconds,
            max_chars=max_segment_chars,
            pause_threshold=pause_threshold,
        )

    return Transcript(
        segments=segments,
        language=detected_language,
        backend=chosen.name,
        model=getattr(chosen, "model", None) or getattr(chosen, "model_size", None),
        source_duration=duration,
        chunk_count=len(chunks),
    )
