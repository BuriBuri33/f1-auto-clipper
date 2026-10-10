"""9:16 vertical reframing (spec section 15: "configurable vertical reframing").

Two strategies:

  * ``center_crop`` — crop the source to the target aspect ratio around the
    middle, then scale. Keeps full resolution; discards the left/right
    edges. The default, because broadcast footage is framed on the action
    already.
  * ``blur_pad`` — scale the WHOLE frame to fit the target width, letterboxed
    top and bottom on a blurred, zoomed copy of itself. Never crops anything
    out.

``center_crop`` needs the source to be at least as wide (relative to its
height) as the target aspect ratio. A source that is already narrower than
9:16 would need a NEGATIVE crop width, which is nonsensical, not just a bad
look — so that case always falls back to ``blur_pad`` instead of producing a
broken filter graph. That fallback is what spec section 15 means by "robust
fallback over bad crops": the only case where the configured strategy is
overridden is one FFmpeg could not execute at all, not a subjective judgement
about which crop "looks bad".
"""

from __future__ import annotations

from typing import Optional, Tuple

from .types import ReframeDecision

VALID_STRATEGIES = ("center_crop", "blur_pad")


def crop_box(
    source_width: int, source_height: int, target_width: int, target_height: int
) -> Optional[Tuple[int, int, int, int]]:
    """``(width, height, x, y)`` for a centred crop at the target aspect ratio.

    Returns ``None`` when the source is already narrower than the target
    ratio — cropping its width further is not possible. Dimensions are
    rounded down to even numbers, which libx264's 4:2:0 chroma subsampling
    requires.
    """
    if source_width <= 0 or source_height <= 0:
        return None
    crop_w = int(round(source_height * target_width / target_height))
    crop_w -= crop_w % 2
    if crop_w <= 0 or crop_w > source_width:
        return None
    crop_h = source_height - (source_height % 2)
    crop_x = (source_width - crop_w) // 2
    crop_x -= crop_x % 2
    return (crop_w, crop_h, crop_x, 0)


def decide(
    strategy: str, source_width: int, source_height: int,
    target_width: int, target_height: int,
) -> ReframeDecision:
    """Resolve the configured strategy against a real source's geometry."""
    if strategy not in VALID_STRATEGIES:
        raise ValueError(
            "video.reframe_strategy must be one of %s, got %r"
            % (VALID_STRATEGIES, strategy)
        )
    if strategy == "blur_pad":
        return ReframeDecision("blur_pad", "configured")

    box = crop_box(source_width, source_height, target_width, target_height)
    if box is None:
        return ReframeDecision(
            "blur_pad",
            "source is %dx%d, already narrower than the %dx%d target ratio — "
            "a centred crop would need a negative width, so blur_pad is used "
            "instead" % (source_width, source_height, target_width, target_height),
        )
    return ReframeDecision("center_crop", "configured")


def filter_clause(
    decision: ReframeDecision,
    source_width: int, source_height: int,
    target_width: int, target_height: int,
    input_label: str = "0:v",
) -> str:
    """One ``filter_complex`` stage (without the trailing output label).

    Caller appends ``[label]`` and chains further stages onto it.
    """
    if decision.strategy == "center_crop":
        box = crop_box(source_width, source_height, target_width, target_height)
        if box is None:
            raise ValueError(
                "decide() should have fallen back to blur_pad for this geometry"
            )
        cw, ch, cx, cy = box
        return (
            "[%s]crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos,setsar=1"
            % (input_label, cw, ch, cx, cy, target_width, target_height)
        )

    # blur_pad: a blurred, over-scaled copy of the WHOLE frame fills the
    # canvas; the frame itself is scaled to fit the width and centred on top.
    return (
        "[%s]split=2[bg][fg];"
        "[bg]scale=%d:%d:force_original_aspect_ratio=increase,"
        "crop=%d:%d,gblur=sigma=20[bg];"
        "[fg]scale=%d:-2:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto"
        % (input_label, target_width, target_height, target_width, target_height,
           target_width)
    )
