"""Transcript types — the contract between transcription and everything after it.

The shape is fixed by the specification (section 9):

    {"segments": [{"start": 123.4, "end": 128.7, "text": "..."}]}

Times are **seconds from the start of the source video**, as floats. Every
backend normalises to this, and every consumer (clip selection, captions)
reads only this — which is what makes the transcription layer genuinely
replaceable rather than nominally so.

Seconds-as-float is the spec's choice and it is right here: Whisper emits
float seconds, the clip-selection JSON uses float seconds, and captions are
rendered from float seconds. (Clip *storage* uses integer milliseconds, for
a different reason — a UNIQUE constraint over floats is unreliable. The
conversion happens at that one boundary.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class Word:
    """One timed word. Optional, but the later phases depend on it.

    Measured 2026-10-10 against synthesised audio with known timing: with
    word timestamps off, Whisper's SEGMENT end times are wrong by seconds —
    a segment stretches across the following silence to meet the next
    utterance (an utterance truly ending at 3.68s was reported as ending at
    7.78s). With word timestamps on, the same segment came back as
    1.62-3.74. So word timing is not a nicety for captions; it is what makes
    the segment boundaries in a ``Transcript`` true.
    """

    start: float
    end: float
    text: str

    def shifted(self, offset: float) -> "Word":
        return Word(self.start + offset, self.end + offset, self.text)

    def as_dict(self) -> Dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "text": self.text}


@dataclass(frozen=True)
class Segment:
    """One timed span of speech, optionally with per-word timing."""

    start: float
    end: float
    text: str
    words: Optional[List[Word]] = None

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(
                "segment ends before it starts: %.3f -> %.3f" % (self.start, self.end)
            )

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0

    def shifted(self, offset: float) -> "Segment":
        """Return this segment moved along the timeline by ``offset`` seconds.

        Used to map a chunk-relative timestamp back onto the source
        timeline. This is the single most error-prone operation in the
        transcription layer, so it is one named method with one test suite
        rather than arithmetic scattered through the backends.
        """
        return Segment(
            self.start + offset,
            self.end + offset,
            self.text,
            [w.shifted(offset) for w in self.words] if self.words else None,
        )

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }
        # Omitted entirely when absent, so the on-disk shape stays exactly
        # the three keys the spec names for a backend that cannot supply them.
        if self.words:
            payload["words"] = [w.as_dict() for w in self.words]
        return payload


@dataclass(frozen=True)
class Transcript:
    """A full transcript, plus provenance about how it was produced."""

    segments: List[Segment] = field(default_factory=list)
    language: Optional[str] = None
    backend: Optional[str] = None
    model: Optional[str] = None
    source_duration: Optional[float] = None
    chunk_count: int = 1

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def covered_seconds(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def is_empty(self) -> bool:
        return not any(s.text.strip() for s in self.segments)

    @property
    def has_word_timings(self) -> bool:
        """Whether per-word timing is available for captions and boundary snapping."""
        return any(s.words for s in self.segments)

    @property
    def words(self) -> List[Word]:
        """Every word across every segment, in order."""
        out: List[Word] = []
        for segment in self.segments:
            if segment.words:
                out.extend(segment.words)
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "segments": [s.as_dict() for s in self.segments],
            "language": self.language,
            "backend": self.backend,
            "model": self.model,
            "source_duration": self.source_duration,
            "chunk_count": self.chunk_count,
            "has_word_timings": self.has_word_timings,
        }

    def to_json(self, indent: int = 1) -> str:
        return json.dumps(self.as_dict(), indent=indent, ensure_ascii=False)

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Transcript":
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list):
            raise ValueError("transcript payload has no 'segments' list")
        segments = []
        for item in raw_segments:
            raw_words = item.get("words") or None
            words = (
                [Word(float(w["start"]), float(w["end"]), str(w.get("text", "")))
                 for w in raw_words]
                if raw_words else None
            )
            segments.append(
                Segment(float(item["start"]), float(item["end"]),
                        str(item.get("text", "")), words)
            )
        return cls(
            segments=segments,
            language=payload.get("language"),
            backend=payload.get("backend"),
            model=payload.get("model"),
            source_duration=payload.get("source_duration"),
            chunk_count=int(payload.get("chunk_count", 1)),
        )

    @classmethod
    def read(cls, path: Path) -> "Transcript":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


class TranscriptionError(Exception):
    """Transcription could not be completed."""


class TranscriptionQuotaExceeded(TranscriptionError):
    """The provider's free allowance is spent. Retry later, never pay."""


def normalise(segments: Iterable[Segment], source_duration: Optional[float] = None
              ) -> List[Segment]:
    """Sort, drop empties, and clamp segments into a sane timeline.

    Whisper backends emit the occasional pathological segment — zero-length,
    out of order, or running past the end of the audio (hallucinated tails at
    the end of a file are a known Whisper behaviour). Fixing them here means
    downstream code can assume a clean, ordered, non-overlapping-ish timeline.
    """
    cleaned: List[Segment] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        start = max(0.0, segment.start)
        end = segment.end
        if source_duration is not None:
            # A small tolerance: backends routinely overshoot by a frame or two.
            limit = source_duration + 0.5
            if start >= limit:
                continue          # entirely past the end — a hallucinated tail
            end = min(end, limit)
        if end <= start:
            continue
        words = segment.words
        if words:
            words = [w for w in words if w.end > w.start and w.text.strip()]
            words = words or None
        cleaned.append(Segment(start, end, text, words))

    cleaned.sort(key=lambda s: (s.start, s.end))
    return cleaned


# --- re-segmentation --------------------------------------------------------

SENTENCE_ENDINGS = (".", "!", "?", "…")


def resegment_from_words(
    segments: List[Segment],
    max_seconds: float = 8.0,
    max_chars: int = 180,
    pause_threshold: float = 0.8,
) -> List[Segment]:
    """Rebuild segments from word timings, ignoring the backend's grouping.

    **Why not just use the backend's segments.** Measured 2026-10-10 on audio
    with known timing: the same speech, same model, gave three segments when
    read from a WAV and ONE 15-second segment when read from the 32 kbit/s MP3
    the pipeline actually produces. The word timings were correct and
    identical in both cases. So Whisper's segment grouping is a function of
    the encoding as much as the speech, while its word timings are stable —
    which makes grouping the wrong thing to depend on.

    Rebuilding from words puts segmentation under our control and makes it
    deterministic. It is also what the later phases need: clip selection must
    not start a clip mid-sentence (spec section 10), and captions must break
    into short readable phrases (section 16). Both are boundary decisions, and
    both are now decided here by one rule rather than inherited from an
    encoder's behaviour.

    A segment is closed at the first of: sentence-ending punctuation, a pause
    longer than ``pause_threshold``, ``max_seconds`` elapsed, or ``max_chars``
    accumulated. Segments without word timings pass through untouched.
    """
    output: List[Segment] = []

    for segment in segments:
        if not segment.words:
            output.append(segment)
            continue

        current: List[Word] = []
        for index, word in enumerate(segment.words):
            current.append(word)
            text = " ".join(w.text for w in current).strip()
            span = current[-1].end - current[0].start

            next_word = (
                segment.words[index + 1] if index + 1 < len(segment.words) else None
            )
            gap = (next_word.start - word.end) if next_word else 0.0

            ends_sentence = word.text.rstrip().endswith(SENTENCE_ENDINGS)
            too_long = span >= max_seconds
            too_wide = len(text) >= max_chars
            long_pause = next_word is not None and gap >= pause_threshold

            if next_word is None or ends_sentence or too_long or too_wide or long_pause:
                output.append(
                    Segment(current[0].start, current[-1].end, text, list(current))
                )
                current = []

        if current:
            text = " ".join(w.text for w in current).strip()
            output.append(
                Segment(current[0].start, current[-1].end, text, list(current))
            )

    return [s for s in output if s.text.strip()]
