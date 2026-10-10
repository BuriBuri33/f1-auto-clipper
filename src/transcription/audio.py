"""Audio extraction and chunking (the spec's "Audio Extraction" stage).

Two jobs, both driven by one hard constraint: **Groq's free tier caps an
upload at 25 MB**, and that cap — not the daily seconds allowance — is what
actually binds. A three-hour source exceeds it at any usable bitrate, so
long audio must be split and the pieces' timestamps mapped back onto the
source timeline.

Encoding choice: 16 kHz mono MP3 at 32 kbit/s. Reasoning:

* Whisper resamples to 16 kHz mono internally and Groq's docs say their
  service downsamples to 16 kHz mono before transcribing, so anything higher
  is bytes spent on information the model discards.
* At 32 kbit/s that is 4 kB/s, so 25 MB holds about 104 minutes — a 180-minute
  source becomes two chunks rather than nine. Lossless FLAC at the same rate
  would be ~21 minutes per chunk.
* MP3 over Opus (which would be smaller still) for decoder reliability: MP3 is
  the most universally accepted of the formats the API lists. Configurable if
  you want to trade that.

Chunk boundaries are the part that breaks silently, so they are handled
explicitly: chunks overlap, and each chunk owns an *authoritative window*
whose edges sit at the middle of the overlap. A segment is kept by exactly
one chunk — the one whose authoritative window contains the segment's
midpoint — which gives no duplicates and no gaps without needing to compare
text.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .types import Segment, TranscriptionError

# Whisper's native rate. Anything above this is discarded by the model.
SAMPLE_RATE = 16000
DEFAULT_BITRATE_KBPS = 32
DEFAULT_OVERLAP_SECONDS = 4.0

# Stay clear of the provider's cap: encoder output size is predictable from
# the bitrate but not exact (headers, padding, the final partial frame), and
# a request rejected for being 2% over costs a whole retry.
SIZE_SAFETY_FACTOR = 0.90


class AudioError(TranscriptionError):
    """Audio could not be extracted or split."""


@dataclass(frozen=True)
class AudioChunk:
    """One piece of audio, with the information needed to re-time it.

    ``source_start`` is where this chunk begins in the SOURCE video's
    timeline, which is the offset to add to every timestamp the backend
    returns. ``window_start`` / ``window_end`` are the authoritative window:
    the span of source time this chunk — and only this chunk — is responsible
    for.
    """

    path: Path
    index: int
    source_start: float
    duration: float
    window_start: float
    window_end: float

    @property
    def source_end(self) -> float:
        return self.source_start + self.duration

    def owns(self, segment: Segment) -> bool:
        """Whether this chunk is responsible for a segment, by its midpoint.

        Midpoint rather than start: a segment straddling the boundary is
        assigned to whichever chunk holds most of it, and a segment is never
        counted twice because the windows do not overlap.
        """
        midpoint = segment.midpoint
        return self.window_start <= midpoint < self.window_end


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(cmd: Sequence[str], what: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
    except OSError as exc:
        raise AudioError("could not run %s: %s" % (what, exc)) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = "\n  ".join(stderr[-4:]) if stderr else "(no stderr)"
        raise AudioError("%s failed (exit %d):\n  %s" % (what, result.returncode, tail))
    return result


# An encoded chunk smaller than this contains no audio frames — it is what
# FFmpeg writes when the requested range lies past the end of the audio
# stream. Such a file is not decodable and must not reach a backend.
MIN_DECODABLE_CHUNK_BYTES = 1024


def probe_duration(media_path: Path) -> float:
    """Duration of the AUDIO in seconds, via ffprobe.

    The audio stream's duration, not the container's — they differ, and the
    difference matters. Observed 2026-10-10: a file whose container reported
    30.60s carried only 19.32s of audio, so chunks planned from the container
    duration ran past the end of the audio and FFmpeg emitted 224-byte files
    with no frames in them, which no decoder will open.

    Falls back to the container duration when no audio stream duration is
    reported (some containers only carry it at the format level).
    """
    if not shutil.which("ffprobe"):
        raise AudioError("ffprobe is not on PATH (it ships with FFmpeg)")
    result = _run(
        ["ffprobe", "-v", "error",
         "-select_streams", "a:0",
         "-show_entries", "stream=duration:format=duration",
         "-print_format", "json", str(media_path)],
        "ffprobe",
    )
    try:
        payload = json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise AudioError("ffprobe gave no usable output for %s" % media_path) from exc

    streams = payload.get("streams") or []
    for stream in streams:
        value = stream.get("duration")
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration

    try:
        return float((payload.get("format") or {})["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AudioError(
            "ffprobe gave no usable duration for %s" % media_path
        ) from exc


def bytes_per_second(bitrate_kbps: int) -> float:
    """Encoded bytes per second of audio at a given bitrate.

    Taken from the bitrate we *ask the encoder for*, never measured from a
    finished file. Measured averages mislead badly: a file that is largely
    silence encodes small overall while its speech-dense regions encode at
    the full nominal rate, so a chunk planned from the average overshoots.
    Observed 2026-10-10: a file averaging 2,544 B/s produced a chunk at
    4,040 B/s — 43% over the predicted size, and over the provider's cap.
    """
    return bitrate_kbps * 1000.0 / 8.0


def extract_audio(
    media_path: Path,
    out_dir: Path,
    bitrate_kbps: int = DEFAULT_BITRATE_KBPS,
    sample_rate: int = SAMPLE_RATE,
    start: Optional[float] = None,
    duration: Optional[float] = None,
    name: Optional[str] = None,
) -> Path:
    """Extract audio as 16 kHz mono MP3, optionally just a time range.

    The range arguments are what let a chunk be encoded **directly from the
    source** rather than cut out of an intermediate file. That matters twice
    over: the output size is predictable because we choose the bitrate, and
    the audio goes through exactly one lossy generation instead of two.
    """
    media_path = Path(media_path)
    if not media_path.exists():
        raise AudioError("media file does not exist: %s" % media_path)
    if not ffmpeg_available():
        raise AudioError(
            "FFmpeg is not on PATH — needed to extract audio.\n"
            "  macOS: brew install ffmpeg / Ubuntu: apt-get install -y ffmpeg"
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (name or ("%s.audio.mp3" % media_path.stem))

    cmd = ["ffmpeg", "-nostdin", "-y"]
    if start is not None:
        # Before -i: seek the input, which is much faster on a long file.
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-i", str(media_path)]
    if duration is not None:
        cmd += ["-t", "%.3f" % duration]
    cmd += [
        "-vn",                                  # drop video
        "-ac", "1",                             # mono
        "-ar", str(sample_rate),
        "-b:a", "%dk" % bitrate_kbps,
        "-map_metadata", "-1",
        str(out_path),
    ]
    _run(cmd, "ffmpeg audio extraction")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise AudioError("audio extraction produced no output for %s" % media_path)
    return out_path


def plan_chunks(
    duration: float,
    max_bytes: int,
    bitrate_kbps: int = DEFAULT_BITRATE_KBPS,
    overlap: float = DEFAULT_OVERLAP_SECONDS,
) -> List[tuple]:
    """Work out chunk spans without touching the filesystem.

    Returns a list of ``(source_start, chunk_duration, window_start,
    window_end)``. Pure arithmetic, so the boundary logic — the part that
    breaks — is testable without encoding anything.

    Sized from the bitrate we will ask the encoder for, not from any measured
    file size. See ``bytes_per_second``.
    """
    if duration <= 0:
        raise AudioError("cannot chunk audio of duration %r" % duration)
    if bitrate_kbps <= 0:
        raise AudioError("bitrate must be positive, got %r" % bitrate_kbps)

    budget = int(max_bytes * SIZE_SAFETY_FACTOR)
    rate = bytes_per_second(bitrate_kbps)
    if duration * rate <= budget:
        return [(0.0, duration, 0.0, duration)]

    chunk_seconds = budget / rate
    if chunk_seconds <= overlap * 2:
        raise AudioError(
            "the %d MB limit allows only %.1fs per chunk at this bitrate, "
            "which is too short to overlap by %.1fs. Lower "
            "transcription.bitrate_kbps or raise the size limit."
            % (max_bytes // (1024 * 1024), chunk_seconds, overlap)
        )

    # Each chunk advances by (chunk - overlap) so consecutive chunks share
    # `overlap` seconds of audio.
    stride = chunk_seconds - overlap
    count = int(math.ceil((duration - overlap) / stride))
    count = max(1, count)

    plans: List[tuple] = []
    for index in range(count):
        source_start = index * stride
        chunk_duration = min(chunk_seconds, duration - source_start)
        if chunk_duration <= 0:
            break
        # Authoritative window: trimmed by half the overlap on any side that
        # has a neighbour, so adjacent windows meet exactly once.
        window_start = source_start if index == 0 else source_start + overlap / 2.0
        is_last = (source_start + chunk_duration) >= duration - 1e-6
        window_end = (
            source_start + chunk_duration
            if is_last
            else source_start + chunk_duration - overlap / 2.0
        )
        plans.append((source_start, chunk_duration, window_start, window_end))
        if is_last:
            break
    return plans


def prepare_chunks(
    media_path: Path,
    out_dir: Path,
    max_bytes: Optional[int],
    duration: Optional[float] = None,
    bitrate_kbps: int = DEFAULT_BITRATE_KBPS,
    overlap: float = DEFAULT_OVERLAP_SECONDS,
) -> List[AudioChunk]:
    """Produce audio chunks from a media file, each fitting under ``max_bytes``.

    Each chunk is encoded **from the source media**, not cut out of an
    intermediate audio file. That is what makes the output size predictable
    (we pick the bitrate) and keeps the audio to a single lossy generation.

    ``max_bytes`` of None means the backend has no upload limit, so the whole
    audio track is one chunk.
    """
    media_path = Path(media_path)
    duration = duration if duration is not None else probe_duration(media_path)
    out_dir = Path(out_dir)

    if max_bytes is None:
        whole = extract_audio(media_path, out_dir, bitrate_kbps=bitrate_kbps)
        return [AudioChunk(whole, 0, 0.0, duration, 0.0, duration)]

    plans = plan_chunks(duration, max_bytes, bitrate_kbps, overlap)

    if len(plans) == 1:
        whole = extract_audio(media_path, out_dir, bitrate_kbps=bitrate_kbps)
        actual = whole.stat().st_size
        if actual > max_bytes:
            # The bitrate estimate was optimistic. Rather than fail, split on
            # the size we just measured — which is now a fact, not a guess.
            return _chunk_by_measured_rate(
                media_path, out_dir, max_bytes, duration, actual,
                bitrate_kbps, overlap,
            )
        start, chunk_duration, window_start, window_end = plans[0]
        return [AudioChunk(whole, 0, start, chunk_duration, window_start, window_end)]

    chunks: List[AudioChunk] = []
    for index, (start, chunk_duration, window_start, window_end) in enumerate(plans):
        chunk_path = extract_audio(
            media_path, out_dir, bitrate_kbps=bitrate_kbps,
            start=start, duration=chunk_duration,
            name="%s.part%02d.mp3" % (media_path.stem, index),
        )
        size = chunk_path.stat().st_size
        if size > max_bytes:
            raise AudioError(
                "chunk %d came out at %d bytes, over the %d byte limit, even "
                "though %d kbit/s predicted %d. Lower "
                "transcription.bitrate_kbps."
                % (index, size, max_bytes, bitrate_kbps,
                   int(chunk_duration * bytes_per_second(bitrate_kbps)))
            )
        if size < MIN_DECODABLE_CHUNK_BYTES:
            # No audio frames: the range lies past the end of the audio
            # stream. Dropping it is correct — there is nothing there to
            # transcribe — and handing it to a decoder would fail the run.
            chunk_path.unlink(missing_ok=True)
            continue
        chunks.append(
            AudioChunk(chunk_path, index, start, chunk_duration,
                       window_start, window_end)
        )

    if not chunks:
        raise AudioError(
            "no decodable audio chunks were produced from %s — the file may "
            "have no audio stream" % media_path.name
        )

    # Dropping tail chunks would leave the final window short of the planned
    # end, so the last surviving chunk takes responsibility through to it.
    last = chunks[-1]
    planned_end = plans[-1][3]
    if last.window_end < planned_end:
        chunks[-1] = AudioChunk(
            last.path, last.index, last.source_start, last.duration,
            last.window_start, planned_end,
        )
    return chunks


def _chunk_by_measured_rate(
    media_path: Path,
    out_dir: Path,
    max_bytes: int,
    duration: float,
    measured_bytes: int,
    bitrate_kbps: int,
    overlap: float,
) -> List[AudioChunk]:
    """Re-plan using a rate measured from a real encode of this very file."""
    effective_kbps = int(math.ceil((measured_bytes / duration) * 8 / 1000.0))
    plans = plan_chunks(duration, max_bytes, max(effective_kbps, 1), overlap)
    chunks: List[AudioChunk] = []
    for index, (start, chunk_duration, window_start, window_end) in enumerate(plans):
        chunk_path = extract_audio(
            media_path, out_dir, bitrate_kbps=bitrate_kbps,
            start=start, duration=chunk_duration,
            name="%s.part%02d.mp3" % (media_path.stem, index),
        )
        if chunk_path.stat().st_size > max_bytes:
            raise AudioError(
                "chunk %d still exceeds %d bytes after re-planning at "
                "%d kbit/s — lower transcription.bitrate_kbps."
                % (index, max_bytes, effective_kbps)
            )
        chunks.append(
            AudioChunk(chunk_path, index, start, chunk_duration,
                       window_start, window_end)
        )
    return chunks


def merge_chunk_segments(
    chunks: Sequence[AudioChunk],
    per_chunk_segments: Sequence[Sequence[Segment]],
) -> List[Segment]:
    """Shift each chunk's segments onto the source timeline and merge them.

    Two steps, in this order:

    1. **Shift.** A backend reports times relative to the chunk it was given,
       so every timestamp gains ``chunk.source_start``. Forgetting this on a
       single chunk produces a transcript that is perfectly plausible and
       entirely wrong after the first boundary.
    2. **Select.** Each shifted segment is kept by the one chunk whose
       authoritative window holds its midpoint, so overlapping audio does not
       yield duplicated speech.
    """
    if len(chunks) != len(per_chunk_segments):
        raise AudioError(
            "have %d chunks but %d segment lists" % (len(chunks), len(per_chunk_segments))
        )

    merged: List[Segment] = []
    for chunk, segments in zip(chunks, per_chunk_segments):
        for segment in segments:
            shifted = segment.shifted(chunk.source_start)
            if chunk.owns(shifted):
                merged.append(shifted)
    merged.sort(key=lambda s: (s.start, s.end))
    return merged
