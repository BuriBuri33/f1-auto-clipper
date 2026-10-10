"""Pre-publish quality gate (spec section 28: "all checks must pass before
publish").

Runs against the FINISHED file via ``ffmpeg.probe`` — checking what FFmpeg
actually produced, not what the command line asked for, because a filter
graph can silently drop a stream (e.g. an encoder falling back to a default
pixel format, or an input with no audio track) without FFmpeg treating that
as an error.

Every failure is collected rather than stopping at the first, so a rejected
clip's ``status_detail`` explains everything wrong at once instead of one
failure hiding the next behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Below this, the file is certainly not a real encoded clip — a safeguard
# against a near-empty or truncated output passing every other check by
# coincidence (e.g. a single black frame can satisfy codec/resolution
# checks).
MIN_PLAUSIBLE_BYTES = 50_000

DURATION_TOLERANCE_SECONDS = 1.0
FPS_TOLERANCE = 0.5


@dataclass
class GateResult:
    failures: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def reason(self) -> str:
        return "; ".join(self.failures)


def _parse_r_frame_rate(value: Optional[str]) -> Optional[float]:
    """ffprobe reports frame rate as "30/1" or "30000/1001", not a float."""
    if not value:
        return None
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            num_f, den_f = float(num), float(den)
        except ValueError:
            return None
        return num_f / den_f if den_f else None
    try:
        return float(value)
    except ValueError:
        return None


def check(
    path: Path,
    probe_data: Dict[str, Any],
    expected_width: int,
    expected_height: int,
    expected_fps: float,
    expected_video_codec: str,
    expected_audio_codec: str,
    expected_duration: float,
    duration_tolerance: float = DURATION_TOLERANCE_SECONDS,
) -> GateResult:
    result = GateResult()
    path = Path(path)

    if not path.exists():
        result.failures.append("output file does not exist")
        return result

    size = path.stat().st_size
    if size < MIN_PLAUSIBLE_BYTES:
        result.failures.append(
            "output is only %d bytes, below the %d byte plausibility floor"
            % (size, MIN_PLAUSIBLE_BYTES)
        )

    streams = probe_data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

    if len(video_streams) != 1:
        result.failures.append("expected exactly 1 video stream, found %d" % len(video_streams))
    if len(audio_streams) != 1:
        result.failures.append("expected exactly 1 audio stream, found %d" % len(audio_streams))

    if video_streams:
        v = video_streams[0]
        if (v.get("width"), v.get("height")) != (expected_width, expected_height):
            result.failures.append(
                "resolution is %sx%s, expected %dx%d"
                % (v.get("width"), v.get("height"), expected_width, expected_height)
            )
        if v.get("codec_name") != expected_video_codec:
            result.failures.append(
                "video codec is %r, expected %r" % (v.get("codec_name"), expected_video_codec)
            )
        fps = _parse_r_frame_rate(v.get("r_frame_rate"))
        if fps is None or abs(fps - expected_fps) > FPS_TOLERANCE:
            result.failures.append(
                "frame rate is %r, expected %.2f" % (v.get("r_frame_rate"), expected_fps)
            )

    if audio_streams:
        a = audio_streams[0]
        if a.get("codec_name") != expected_audio_codec:
            result.failures.append(
                "audio codec is %r, expected %r" % (a.get("codec_name"), expected_audio_codec)
            )

    duration_raw = (probe_data.get("format") or {}).get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        duration = None
    if duration is None:
        result.failures.append("could not determine output duration")
    elif abs(duration - expected_duration) > duration_tolerance:
        result.failures.append(
            "duration is %.2fs, expected %.2fs +/- %.2fs"
            % (duration, expected_duration, duration_tolerance)
        )

    return result
