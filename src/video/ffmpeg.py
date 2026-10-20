"""Thin subprocess wrapper around FFmpeg/ffprobe for the render stage.

Rendering is FFmpeg via subprocess and nothing else — no new pip dependency
(see requirements.txt: "Phase 5 — no dependency, rendering is FFmpeg via
subprocess"). Every filter used by this stage (``crop``, ``gblur``,
``overlay``, ``drawtext``, ``ass``) was verified present and working against
a real build before anything here relied on it — see docs/LIMITS.md section
8a for what that measurement found and why it mattered.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from .types import RenderError


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(
    cmd: Sequence[str], what: str, timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=timeout,
        )
    except OSError as exc:
        raise RenderError("could not run %s: %s" % (what, exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError("%s timed out after %ss" % (what, timeout)) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = "\n  ".join(stderr[-8:]) if stderr else "(no stderr)"
        raise RenderError("%s failed (exit %d):\n  %s" % (what, result.returncode, tail))
    return result


# What this stage actually uses. ``ass`` for captions, ``drawtext`` for the
# watermark, the rest for the two reframe strategies.
REQUIRED_FILTERS = ("ass", "drawtext", "overlay", "crop", "gblur")


def available_filters() -> Set[str]:
    """Filter names this FFmpeg build actually registers.

    **Why this exists, not just ``ffmpeg_available()``.** Measured
    2026-10-10: a plain Homebrew ``ffmpeg`` install has neither ``ass``/
    ``subtitles`` nor even ``drawtext`` — both need libass/libfreetype,
    which that formula does not bundle (the separate ``ffmpeg-full``
    formula does). A present ``ffmpeg`` binary therefore proves nothing
    about caption or watermark support; parsing ``ffmpeg -filters`` is what
    actually proves it.
    """
    if not ffmpeg_available():
        return set()
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return parse_filter_names(result.stdout.decode("utf-8", "replace"))


def parse_filter_names(listing: str) -> Set[str]:
    """Parse ``ffmpeg -filters`` output into a set of filter names.

    Pulled out as a pure function so the parsing itself is testable without
    an FFmpeg binary. A filter line looks like::

        TSC overlay           VV->V      Overlay a video source...

    — a 2-3 character flag field (T/S/C/.), then the name. Lines are matched
    by that shape rather than by position after a "------" separator,
    because Ubuntu 24.04's FFmpeg 6.1 prints no separator at all (measured
    on a GitHub runner 2026-10-20): keying on it read zero filters there and
    reported even `crop` as missing. Legend lines ("T.. = Timeline support")
    share the flag shape but have "=" as their second field.
    """
    names: Set[str] = set()
    for line in listing.splitlines():
        parts = line.split()
        if (
            len(parts) >= 3
            and _FLAG_FIELD.fullmatch(parts[0])
            and parts[1] != "="
        ):
            names.add(parts[1])
    return names


_FLAG_FIELD = re.compile(r"[TSC.]{2,3}")


def missing_required_filters() -> List[str]:
    present = available_filters()
    return [f for f in REQUIRED_FILTERS if f not in present]


def probe(path: Path) -> Dict[str, Any]:
    """ffprobe a finished file: duration, plus every stream's codec/geometry."""
    if not shutil.which("ffprobe"):
        raise RenderError("ffprobe is not on PATH (it ships with FFmpeg)")
    result = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,duration:"
            "format=duration",
            "-print_format", "json", str(path),
        ],
        "ffprobe",
    )
    try:
        return json.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RenderError("ffprobe gave no usable output for %s" % path) from exc


def run_ffmpeg(args: Sequence[str], timeout: Optional[float] = 300) -> None:
    """Run one FFmpeg command. ``args`` is everything after the binary name.

    ``-nostdin`` (never block waiting for input the caller has none of),
    ``-y`` (overwrite — the output path is always ours, freshly chosen) and
    quiet logging are fixed here so every call site does not have to repeat
    them identically.
    """
    if not ffmpeg_available():
        raise RenderError(
            "FFmpeg is not on PATH.\n"
            "  macOS: brew install ffmpeg / Ubuntu: apt-get install -y ffmpeg"
        )
    _run(
        ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args],
        "ffmpeg render", timeout=timeout,
    )
