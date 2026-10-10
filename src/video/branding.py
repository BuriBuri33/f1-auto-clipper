"""Configurable watermark (spec section 17).

Drawn with ``drawtext`` rather than an image overlay, because the only input
is a short piece of text (``branding.watermark_text``, usually
``${BRAND_NAME}``) and a image asset is one more thing to source and commit.

"Skip it if it would block key footage": rather than attempt to measure what
is "key footage" in a given frame — there is no free saliency model this
project could call — the watermark is kept small and restricted to a single
corner of the frame by construction, so it is structurally incapable of
covering anything central. The one case that still needs runtime handling is
a bottom corner colliding with the caption safe-zone, which is pushed clear
rather than skipped: skipping would silently drop the branding the config
asked for, and the margin math already knows exactly how much room the
captions need.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

VALID_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right")


def _escape_textfile_path(path: Path) -> str:
    """Same escaping ``captions.escape_filter_path`` uses, for the same reason."""
    text = str(path)
    text = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return "'%s'" % text


def write_watermark_textfile(text: str, path: Path) -> Path:
    """Write the watermark text to a file rather than inlining it in the
    filter string.

    ``drawtext``'s ``text=`` argument needs its own escaping for ``:``,
    ``'``, ``%`` and ``\\``, and ``BRAND_NAME`` is operator-supplied content
    (from ``.env`` / a GitHub secret) that should not have to be trusted to
    avoid filter-breaking characters. ``textfile=`` sidesteps that: the file
    contents are drawn literally.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def filter_clause(
    textfile: Path,
    position: str,
    font_size: int = 40,
    margin: int = 40,
    caption_margin_bottom: Optional[int] = None,
    input_label: str = "v",
) -> str:
    """One ``filter_complex`` stage drawing the watermark. No trailing label.

    ``expansion=none`` disables ``drawtext``'s own ``%{...}`` expansion
    syntax, so a brand name that happens to contain a ``%`` is drawn as
    literal text rather than interpreted as an expansion directive.
    """
    if position not in VALID_POSITIONS:
        raise ValueError(
            "branding.position must be one of %s, got %r" % (VALID_POSITIONS, position)
        )

    x = "w-tw-%d" % margin if "right" in position else str(margin)
    if "top" in position:
        y = str(margin)
    else:
        # Clear the caption safe-zone rather than sit on top of it.
        bottom_clearance = margin
        if caption_margin_bottom:
            bottom_clearance = max(margin, caption_margin_bottom + margin)
        y = "h-th-%d" % bottom_clearance

    return (
        "[%s]drawtext=textfile=%s:expansion=none:fontsize=%d:fontcolor=white:"
        "x=%s:y=%s:box=1:boxcolor=black@0.4:boxborderw=10"
        % (input_label, _escape_textfile_path(textfile), font_size, x, y)
    )
