"""Types for the rendering stage — the contract between a scored clip and a
publishable MP4 (spec sections 14-18, 28).

Everything here is a plain value: the stage that fills them in
(``runner.render_clip``) is the only place FFmpeg is actually invoked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List


class RenderError(Exception):
    """A clip could not be rendered to a usable MP4."""


class QualityGateFailed(RenderError):
    """FFmpeg produced a file, but it fails the pre-publish checks (spec section 28)."""


@dataclass(frozen=True)
class ReframeDecision:
    """Which 9:16 strategy was actually used, and why.

    Separate from the configured strategy because ``center_crop`` can fall
    back to ``blur_pad`` for a source whose own aspect ratio makes a crop
    degenerate (spec section 15: "robust fallback over bad crops").
    """

    strategy: str          # "center_crop" | "blur_pad"
    reason: str


@dataclass(frozen=True)
class CaptionCue:
    """One burned-caption line, in CLIP-relative seconds (0 = clip start)."""

    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                "caption cue ends before it starts: %.3f -> %.3f"
                % (self.start, self.end)
            )


@dataclass(frozen=True)
class RenderMetadata:
    """Title/description/hashtags for a rendered clip (spec section 18)."""

    title: str
    description: str
    hashtags: List[str] = field(default_factory=list)

    @property
    def hashtags_text(self) -> str:
        return " ".join("#%s" % tag for tag in self.hashtags)


@dataclass(frozen=True)
class RenderResult:
    """What a successful render produced, for the quality gate and the DB."""

    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    reframe: ReframeDecision
    caption_cue_count: int
    metadata: RenderMetadata
