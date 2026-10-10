"""Groq Whisper backend — the preferred transcription path.

Verified against Groq's docs on 2026-10-10:

* endpoint ``https://api.groq.com/openai/v1/audio/transcriptions``
  (OpenAI-compatible, so an OpenAI-shaped alternative is a base-URL change)
* **25 MB upload cap on the free tier** — the binding constraint, handled by
  chunking in ``audio.py``
* free allowance also ~28,800 audio seconds/day, 20 req/min, 2,000 req/day,
  which one 3-hour video per day sits comfortably inside
* models ``whisper-large-v3-turbo`` (default here) and ``whisper-large-v3``
* ``response_format=verbose_json`` is what returns per-segment timestamps

No paid tier is reachable from this module. A spent allowance raises
``TranscriptionQuotaExceeded`` so the caller can leave the work queued.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .types import (
    Segment,
    TranscriptionError,
    TranscriptionQuotaExceeded,
    Word,
)

API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "whisper-large-v3-turbo"

# Free tier, verified 2026-10-10. The size cap is what binds in practice.
FREE_TIER_MAX_BYTES = 25 * 1024 * 1024
FREE_TIER_AUDIO_SECONDS_PER_DAY = 28_800


class GroqTranscriber:
    """Transcribes one audio file per call. Chunking is the caller's job."""

    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        language: Optional[str] = None,
        timeout: int = 300,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        session=None,
        sleep=time.sleep,
    ) -> None:
        if not api_key:
            raise TranscriptionError("no Groq API key supplied")
        self.api_key = api_key
        self.model = model
        self.language = language
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.session = session or requests
        self.sleep = sleep

    @property
    def max_bytes(self) -> int:
        return FREE_TIER_MAX_BYTES

    def transcribe_file(self, audio_path: Path) -> Dict[str, Any]:
        """POST one audio file and return the parsed verbose_json payload."""
        audio_path = Path(audio_path)
        size = audio_path.stat().st_size
        if size > FREE_TIER_MAX_BYTES:
            # Caught here rather than at the API, because the error the API
            # returns for an oversized body is far less actionable.
            raise TranscriptionError(
                "%s is %.1f MB, over Groq's %d MB free-tier limit. Audio must "
                "be chunked before upload (see audio.split_audio)."
                % (audio_path.name, size / (1024 * 1024),
                   FREE_TIER_MAX_BYTES // (1024 * 1024))
            )

        data = {"model": self.model, "response_format": "verbose_json"}
        # Ask for word granularity as well as segment: without per-word
        # timing, Whisper's segment END times stretch across following
        # silence (measured, see local_backend). Costs nothing extra.
        data["timestamp_granularities[]"] = ["segment", "word"]
        if self.language:
            data["language"] = self.language

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                with audio_path.open("rb") as handle:
                    response = self.session.post(
                        API_URL,
                        headers={"Authorization": "Bearer %s" % self.api_key},
                        files={"file": (audio_path.name, handle, "audio/mpeg")},
                        data=data,
                        timeout=self.timeout,
                    )
            except requests.RequestException as exc:
                last_error = TranscriptionError("Groq request failed: %s" % exc)
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise TranscriptionError(
                            "Groq returned a non-JSON body: %s" % exc
                        ) from exc

                body = self._body(response)

                if response.status_code == 401:
                    raise TranscriptionError(
                        "Groq rejected the key (401). Check TRANSCRIPTION_API_KEY; "
                        "get one free at https://console.groq.com/keys"
                    )
                if response.status_code == 429:
                    # Two very different things share this code: a per-minute
                    # rate limit (wait seconds, retry) and a spent daily
                    # allowance (wait hours — never retry in-run).
                    if self._is_daily_limit(body, response):
                        raise TranscriptionQuotaExceeded(
                            "Groq's free daily allowance is spent (about "
                            "%d audio seconds/day). The video stays queued and "
                            "the next run picks it up; nothing is lost and "
                            "nothing is charged."
                            % FREE_TIER_AUDIO_SECONDS_PER_DAY
                        )
                    last_error = TranscriptionError("Groq rate limited (429)")
                elif response.status_code == 413:
                    raise TranscriptionError(
                        "Groq rejected the upload as too large (413) even "
                        "though it passed the local size check — lower "
                        "transcription.bitrate_kbps."
                    )
                elif response.status_code >= 500:
                    last_error = TranscriptionError(
                        "Groq returned HTTP %d" % response.status_code
                    )
                else:
                    raise TranscriptionError(
                        "Groq returned HTTP %d: %s"
                        % (response.status_code, str(body)[:300])
                    )

            if attempt < self.max_retries - 1:
                delay = self.backoff_base * (2 ** attempt)
                self.sleep(delay + random.uniform(0, delay * 0.25))

        raise last_error or TranscriptionError("Groq could not be reached")

    @staticmethod
    def _body(response) -> Any:
        try:
            return response.json()
        except ValueError:
            return getattr(response, "text", "")

    @staticmethod
    def _is_daily_limit(body: Any, response) -> bool:
        """Distinguish a spent daily allowance from a per-minute rate limit."""
        haystack = str(body).lower()
        for marker in ("per day", "daily", "audio seconds", "rpd", "asd", "tokens per day"):
            if marker in haystack:
                return True
        # Groq sends Retry-After; anything beyond a few minutes is not a
        # per-minute limit.
        retry_after = (getattr(response, "headers", {}) or {}).get("retry-after")
        try:
            if retry_after is not None and float(retry_after) > 600:
                return True
        except (TypeError, ValueError):
            pass
        return False


def parse_verbose_json(payload: Dict[str, Any]) -> List[Segment]:
    """Turn a verbose_json body into segments.

    Falls back to one whole-text segment when the response carries no
    segments. That happens for very short audio, and silently returning
    nothing would look identical to silence.
    """
    raw = payload.get("segments")
    if isinstance(raw, list) and raw:
        segments: List[Segment] = []
        for item in raw:
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(item.get("text", "")).strip()
            if end < start:
                continue
            segments.append(Segment(start, end, text, _words_of(item)))
        if segments:
            return segments

    text = str(payload.get("text", "")).strip()
    if not text:
        return []
    duration = payload.get("duration")
    try:
        end = float(duration)
    except (TypeError, ValueError):
        end = 0.0
    if end <= 0:
        return []
    return [Segment(0.0, end, text)]


def _words_of(item: Dict[str, Any]) -> Optional[List[Word]]:
    """Per-word timings from a verbose_json segment, when the API supplied them."""
    raw = item.get("words")
    if not isinstance(raw, list) or not raw:
        return None
    words: List[Word] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            continue
        text = str(entry.get("word", entry.get("text", ""))).strip()
        words.append(Word(start, end, text))
    return words or None
