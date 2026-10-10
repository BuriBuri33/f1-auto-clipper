"""Burned-in captions (spec section 16: mobile-legible, inside safe margins).

Two steps:

1. ``build_cues`` — take the transcript segments overlapping a clip's time
   window, shift them to clip-relative time, and re-chunk them into
   caption-sized cues. Reuses
   ``transcription.types.resegment_from_words`` — the same primitive that
   turns Whisper's word timings into sentence/pause-bounded segments in
   Phase 3 and into candidate-window boundaries in Phase 4 — with tighter
   limits, because a burned caption has to fit in a line or two, not a full
   sentence.
2. ``write_ass`` — render those cues as an ASS subtitle file FFmpeg's
   ``ass`` filter can burn in (see ``video.ffmpeg.REQUIRED_FILTERS``).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..transcription.types import Segment, resegment_from_words
from .types import CaptionCue

DEFAULT_FONT = "Arial"
DEFAULT_FONT_SIZE = 64
DEFAULT_MARGIN_BOTTOM = 220
DEFAULT_MARGIN_SIDE = 80


def build_cues(
    segments: List[Segment],
    clip_start: float,
    clip_end: float,
    max_chars: int = 36,
    max_seconds: float = 4.0,
    pause_threshold: float = 0.5,
) -> List[CaptionCue]:
    """Caption cues for one clip, in clip-relative seconds.

    Only segments that have word timings can be clipped and re-chunked
    accurately; a segment with no words is kept whole if it falls entirely
    inside the clip, and dropped otherwise (there is no sub-segment timing to
    cut it on).
    """
    clip_duration = clip_end - clip_start
    windowed: List[Segment] = []

    for segment in segments:
        if segment.end <= clip_start or segment.start >= clip_end:
            continue
        if not segment.words:
            if segment.start >= clip_start and segment.end <= clip_end:
                windowed.append(segment.shifted(-clip_start))
            continue

        kept = [
            w for w in segment.words
            if w.start >= clip_start and w.end <= clip_end
        ]
        if not kept:
            continue
        text = " ".join(w.text for w in kept).strip()
        if not text:
            continue
        shifted_words = [w.shifted(-clip_start) for w in kept]
        windowed.append(
            Segment(shifted_words[0].start, shifted_words[-1].end, text, shifted_words)
        )

    windowed.sort(key=lambda s: s.start)
    recut = resegment_from_words(
        windowed, max_seconds=max_seconds, max_chars=max_chars,
        pause_threshold=pause_threshold,
    )

    cues: List[CaptionCue] = []
    for segment in recut:
        start = max(0.0, segment.start)
        end = min(clip_duration, segment.end)
        if end <= start:
            continue
        cues.append(CaptionCue(start, end, segment.text))
    return cues


def _escape_ass_text(text: str) -> str:
    """Escape characters ASS's override-code syntax would otherwise read specially.

    ``{`` opens an override block and ``\\N``/``\\n`` are line breaks — a
    transcript containing either verbatim (rare, but Whisper output is not
    controlled input) must not be allowed to alter the subtitle's styling or
    timing. Backslashes are escaped first so the braces' own escaping is not
    double-escaped.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", " ")
    )


def _format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    centiseconds = int(round((secs - int(secs)) * 100))
    if centiseconds == 100:
        centiseconds = 0
        secs += 1
    return "%d:%02d:%02d.%02d" % (hours, minutes, int(secs), centiseconds)


def build_ass_document(
    cues: List[CaptionCue],
    video_width: int,
    video_height: int,
    font: str = DEFAULT_FONT,
    font_size: int = DEFAULT_FONT_SIZE,
    margin_bottom: int = DEFAULT_MARGIN_BOTTOM,
    margin_side: int = DEFAULT_MARGIN_SIDE,
) -> str:
    """Build the full ``.ass`` document as text.

    Style: bold white text, a soft outline plus a semi-transparent box behind
    it (legible over both bright and dark footage without needing per-frame
    contrast analysis), bottom-anchored inside ``margin_bottom`` /
    ``margin_side`` so it clears the UI every Shorts/Reels/TikTok player
    draws over the frame.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: %d\n"
        "PlayResY: %d\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,%s,%d,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "1,0,0,0,100,100,0,0,1,3,0,2,%d,%d,%d,0\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    ) % (video_width, video_height, font, font_size, margin_side, margin_side,
         margin_bottom)

    lines = [header]
    for cue in cues:
        lines.append(
            "Dialogue: 0,%s,%s,Default,,0,0,0,,%s\n"
            % (_format_timestamp(cue.start), _format_timestamp(cue.end),
               _escape_ass_text(cue.text))
        )
    return "".join(lines)


def write_ass(
    cues: List[CaptionCue],
    path: Path,
    video_width: int,
    video_height: int,
    **style_kwargs,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_ass_document(cues, video_width, video_height, **style_kwargs),
        encoding="utf-8",
    )
    return path


def escape_filter_path(path: Path) -> str:
    """Escape a filesystem path for use as an FFmpeg filter argument.

    The ``ass``/``subtitles`` filter's filename argument treats ``:`` and
    ``\\`` specially (``C:\\...`` is the canonical case this breaks on), and
    the whole thing is then wrapped in single quotes to tolerate spaces — so
    an embedded ``'`` must itself be escaped. POSIX temp paths rarely hit
    this, but the escaping is correctness, not a convenience for one platform.
    """
    text = str(path)
    text = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return "'%s'" % text
