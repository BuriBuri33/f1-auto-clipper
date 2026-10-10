"""yt-dlp wrapper: probe, then download.

Three things learned from testing this against YouTube on 2026-10-10, each of
which shapes the code:

1. **Extraction breaks, per player client.** Of five yt-dlp player clients
   tried against one public video, three failed outright ("The page needs to
   be reloaded", "Requested format is not available") and one worked. Which
   client works changes over time as YouTube pushes back. So probing walks a
   fallback chain of clients rather than trusting the default — the same
   pattern as the detector fallback in ``monitoring``.

2. **yt-dlp does not reliably report the licence.** The ``license`` key was
   absent entirely from the extractor output for a known CC-BY video. The
   licence decision therefore comes from the Data API only; see
   ``gate.evaluate``. Nothing here is allowed to influence it.

3. **YouTube serves essentially one progressive format, at 360p.** Everything
   above that is video-only plus separate audio, which must be muxed — so
   **FFmpeg is a hard requirement**, not a Phase 5 concern. Rather than
   silently downloading 360p and upscaling it 3x into a 1080x1920 frame (which
   the spec explicitly forbids), a missing FFmpeg is a clear error.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Clients are tried in this order. `default` first so that a working upstream
# default is used when it works; the rest are the fallbacks that were observed
# to behave differently. Overridable via config, because this list is the part
# most likely to need changing without a code release.
DEFAULT_PLAYER_CLIENTS = ("default", "android_vr", "tv", "web_safari", "mweb", "ios")

# Errors that mean "this client is broken right now", as opposed to "this
# video cannot be downloaded at all". Only the former justifies trying the
# next client; retrying a genuinely unavailable video through six clients is
# six pointless requests.
_TRANSIENT_EXTRACTOR_SIGNS = (
    "page needs to be reloaded",
    "requested format is not available",
    "failed to extract",
    "unable to extract",
    "player response",
    "sign in to confirm",
    "precondition check failed",
    "http error 403",
    "unable to download api page",
)

# Errors that look final. "Look", not "are": a client that cannot see a video
# reports it as unavailable in exactly these words, so one such message is
# NOT evidence the video is gone. Verified 2026-10-10: a probe succeeded via
# android_vr on a video that `mweb` and `web_embedded` both called
# unavailable. Only UNANIMOUS agreement across every client is treated as
# permanent — see `_chain_failure`.
_PERMANENT_EXTRACTOR_SIGNS = (
    # Both wordings occur. "Video unavailable" came from `mweb`, "This video
    # is unavailable" from `web_embedded`, in the same diagnostic run — so
    # matching only the first silently downgrades the second to "unknown".
    "video unavailable",
    "video is unavailable",
    "private video",
    "members-only",
    "this video has been removed",
    "is not available in your country",
    "account associated with this video has been terminated",
    "video is age restricted",
    "drm",
)


class DownloadError(Exception):
    """A probe or download could not be completed."""


class UnavailableVideo(DownloadError):
    """The video itself cannot be fetched — retrying will not help."""


@dataclass(frozen=True)
class ProbeResult:
    """Metadata read without downloading any media."""

    video_id: str
    title: str
    duration_seconds: Optional[int]
    estimated_bytes: Optional[int]
    live_status: Optional[str]
    availability: Optional[str]
    height: Optional[int]
    player_client: str = "default"
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_live(self) -> bool:
        return self.live_status in {"is_live", "is_upcoming", "post_live"}


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int
    format_id: Optional[str]
    player_client: str


def ffmpeg_available() -> bool:
    """Whether FFmpeg is on PATH. Required to mux video-only + audio streams."""
    return shutil.which("ffmpeg") is not None


def format_selector(preferred_max_height: int, allow_progressive_only: bool) -> str:
    """Build a yt-dlp format string.

    Prefers H.264 video + AAC audio in MP4, because that is what the render
    stage and all three publishing targets want, and remuxing later is wasted
    work. Caps height: a 9:16 crop of a 16:9 source loses most of the width,
    so a 1440p source yields a genuine 1080-wide vertical frame while a 1080p
    source has to be upscaled.
    """
    h = preferred_max_height
    if allow_progressive_only:
        # No FFmpeg: only single-file formats are usable. In practice this
        # means 360p on YouTube.
        return "best[vcodec!=none][acodec!=none][ext=mp4]/best[vcodec!=none][acodec!=none]"
    return (
        # 1st choice: H.264 video + AAC audio, both MP4, within the height cap.
        "bestvideo[height<=%d][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/"
        # 2nd: any codec within the cap, still separate streams.
        "bestvideo[height<=%d]+bestaudio/"
        # 3rd: a progressive file, if one exists.
        "best[height<=%d][vcodec!=none][acodec!=none]/"
        # Last: whatever yt-dlp considers best.
        "best" % (h, h, h)
    )


@dataclass(frozen=True)
class _Failure:
    client: str
    kind: str      # 'permanent' | 'transient' | 'unknown'
    message: str


def _chain_failure(what: str, url: str, failures: List["_Failure"]) -> DownloadError:
    """Turn a whole exhausted client chain into one error of the right type.

    The distinction matters because it decides a video's fate: UnavailableVideo
    is terminal (status SKIPPED, never retried) while DownloadError is
    retryable (status FAILED, picked up next pass).

    So the terminal verdict requires UNANIMITY. A single client reporting
    "Video unavailable" is routinely just that client being unable to see the
    video — observed on a video that another client probed successfully in the
    same second. Getting this wrong in the permissive direction costs a few
    cheap requests; getting it wrong in the terminal direction silently drops
    a usable video forever, so the asymmetry decides the rule.
    """
    detail = "\n  ".join(
        "%s [%s]: %s" % (f.client, f.kind, f.message) for f in failures
    )
    if failures and all(f.kind == "permanent" for f in failures):
        return UnavailableVideo(
            "every player client reports %s as unavailable:\n  %s" % (url, detail)
        )
    return DownloadError(
        "every player client failed to %s %s:\n  %s" % (what, url, detail)
    )


def _classify(exc: Exception) -> str:
    """'permanent' | 'transient' | 'unknown' for an extractor error."""
    message = str(exc).lower()
    for sign in _PERMANENT_EXTRACTOR_SIGNS:
        if sign in message:
            return "permanent"
    for sign in _TRANSIENT_EXTRACTOR_SIGNS:
        if sign in message:
            return "transient"
    return "unknown"


def _base_opts(player_client: str) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        # Never write anything outside the working directory we were given.
        "noprogress": True,
        "consoletitle": False,
    }
    if player_client != "default":
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    return opts


def _extract(ydl_factory, url: str, opts: Dict[str, Any], download: bool):
    with ydl_factory(opts) as ydl:
        return ydl.extract_info(url, download=download)


def probe(
    url: str,
    player_clients: Sequence[str] = DEFAULT_PLAYER_CLIENTS,
    ydl_factory=None,
) -> ProbeResult:
    """Read a video's metadata without downloading media.

    Walks ``player_clients`` until one succeeds, then stops. The whole chain
    is tried before any failure verdict, because a single client reporting a
    video as unavailable is not evidence that it is — see ``_chain_failure``.
    """
    ydl_factory = ydl_factory or _default_factory()
    failures: List[_Failure] = []

    for client in player_clients:
        opts = _base_opts(client)
        opts["skip_download"] = True
        try:
            info = _extract(ydl_factory, url, opts, download=False)
        except Exception as exc:
            failures.append(_Failure(client, _classify(exc), str(exc)[:160]))
            continue

        if not info:
            failures.append(_Failure(client, "transient", "extractor returned nothing"))
            continue

        return ProbeResult(
            video_id=info.get("id") or "",
            title=info.get("title") or "(untitled)",
            duration_seconds=_as_int(info.get("duration")),
            estimated_bytes=_as_int(
                info.get("filesize") or info.get("filesize_approx")
            ),
            live_status=info.get("live_status"),
            availability=info.get("availability"),
            height=_as_int(info.get("height")),
            player_client=client,
            raw=info,
        )

    raise _chain_failure("probe", url, failures)


def download(
    url: str,
    dest_dir: Path,
    preferred_max_height: int = 1440,
    max_bytes: Optional[int] = None,
    player_clients: Sequence[str] = DEFAULT_PLAYER_CLIENTS,
    allow_progressive_only: bool = False,
    ydl_factory=None,
) -> DownloadResult:
    """Download one video into ``dest_dir``.

    ``max_bytes`` is passed to yt-dlp as a hard ceiling, so a video whose size
    could not be estimated up front still cannot fill the disk.
    """
    if not allow_progressive_only and not ffmpeg_available():
        raise DownloadError(
            "FFmpeg is not on PATH. YouTube serves only one progressive "
            "(single-file) format and it is 360p, so anything usable for a "
            "1080x1920 render has to be muxed from separate video and audio "
            "streams, which needs FFmpeg.\n"
            "  macOS:  brew install ffmpeg\n"
            "  Ubuntu: sudo apt-get install -y ffmpeg\n"
            "GitHub-hosted ubuntu runners already have it.\n"
            "To proceed anyway at 360p (testing only), set "
            "download.allow_progressive_only: true — but do not render from "
            "it: upscaling 360p into a 1080x1920 frame looks exactly as bad "
            "as it sounds."
        )

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ydl_factory = ydl_factory or _default_factory()
    failures: List[_Failure] = []

    for client in player_clients:
        opts = _base_opts(client)
        opts.update(
            {
                "format": format_selector(preferred_max_height, allow_progressive_only),
                "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
                "merge_output_format": "mp4",
                "overwrites": True,
            }
        )
        if max_bytes:
            opts["max_filesize"] = max_bytes

        try:
            info = _extract(ydl_factory, url, opts, download=True)
        except Exception as exc:
            failures.append(_Failure(client, _classify(exc), str(exc)[:160]))
            continue

        if not info:
            failures.append(_Failure(client, "transient", "extractor returned nothing"))
            continue

        path = _resolve_output_path(info, dest_dir)
        if path is None:
            # yt-dlp reports success but no file exists: the commonest cause
            # is max_filesize aborting the download mid-way.
            failures.append(_Failure(
                client, "transient",
                "no output file (the size ceiling may have aborted it)",
            ))
            continue

        return DownloadResult(
            path=path,
            size_bytes=path.stat().st_size,
            format_id=info.get("format_id"),
            player_client=client,
        )

    raise _chain_failure("download", url, failures)


def _resolve_output_path(info: Dict[str, Any], dest_dir: Path) -> Optional[Path]:
    """Find the file yt-dlp actually wrote.

    yt-dlp reports the final path in several places depending on whether a
    merge or remux happened, and the extension in ``outtmpl`` is not
    necessarily the one on disk. Checked in order of reliability, with a
    directory scan as the last resort.
    """
    candidates: List[str] = []
    requested = info.get("requested_downloads") or []
    for item in requested:
        for key in ("filepath", "_filename", "filename"):
            if item.get(key):
                candidates.append(item[key])
    for key in ("filepath", "_filename", "filename"):
        if info.get(key):
            candidates.append(info[key])

    for candidate in candidates:
        path = Path(candidate)
        if path.exists() and path.stat().st_size > 0:
            return path

    video_id = info.get("id")
    if video_id:
        matches = sorted(
            (p for p in dest_dir.glob("%s.*" % video_id)
             if p.is_file() and p.stat().st_size > 0),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )
        if matches:
            return matches[0]
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _default_factory():
    """Import yt_dlp lazily, so the rest of the package works without it."""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise DownloadError(
            "yt-dlp is not installed. pip install -r requirements.txt"
        ) from exc
    return yt_dlp.YoutubeDL
