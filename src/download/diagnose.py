"""Environment self-test for the download stage.

This exists because of a concrete failure. On 2026-10-10, from one network,
yt-dlp could *probe* a public video (via the ``android_vr`` client) but **no
player client could download its media** — nine were tried, all nine failed,
with 403s and "The page needs to be reloaded". The same network also got
HTTP 404 from YouTube's Atom feed endpoint while channel pages returned 200.

That is not something unit tests can tell you, because it is a property of
the network, the IP's reputation and the installed yt-dlp version — all of
which differ between a laptop, a CI runner and next month. So the capability
is something to *measure where it will run*, not assume.

Run it locally and in CI:

    python -m src.main doctor
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..monitoring import youtube_api
from ..monitoring.types import DetectorError
from . import downloader

# Blender Foundation's Big Buck Bunny: Creative Commons BY, long-lived, and
# small at the lowest format. A fixed known-good target makes a failure a
# statement about the environment rather than about the video.
# The oldest yt-dlp known to fetch YouTube media successfully from a clean
# environment. Measured 2026-10-10: 2025.10.14 failed on all nine player
# clients while 2026.08.19 succeeded on the first, same machine, same video,
# minutes apart. A version floor is therefore a real diagnostic and not
# hygiene — an old yt-dlp presents as "YouTube is blocking us", which sends
# you looking in entirely the wrong place.
MIN_KNOWN_GOOD_YTDLP = (2026, 8, 19)
MIN_KNOWN_GOOD_YTDLP_STR = "2026.08.19"

PROBE_TARGET_ID = "YE7VzlLtp-4"
PROBE_TARGET = "https://www.youtube.com/watch?v=%s" % PROBE_TARGET_ID
# The film is CC-BY, but note its YouTube upload's `status.license` reads
# "youtube" — a standing reminder that the API field records the uploader's
# tickbox, not the work's licence. Used here only as a stable, long-lived,
# small public video, never as a licence example.
PROBE_TARGET_NOTE = "Big Buck Bunny (Blender Foundation)"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))


def _parse_ytdlp_version(version: str):
    """yt-dlp versions are date-based: 2026.08.19, sometimes with a suffix."""
    parts = []
    for piece in version.split(".")[:3]:
        # LEADING digits only. Stripping every non-digit instead would turn
        # "19rc1" into 191, making a release candidate compare as newer than
        # the release it precedes.
        match = re.match(r"\d+", piece)
        if not match:
            return None
        parts.append(int(match.group(0)))
    return tuple(parts) if len(parts) == 3 else None


def _ytdlp_is_too_old(version: str) -> bool:
    """True only when the version is confidently older than the known-good one.

    An unparseable version returns False — a nightly or a fork should not be
    reported as broken on the strength of its version string alone, and the
    byte-fetch check further down is the real evidence either way.
    """
    parsed = _parse_ytdlp_version(version)
    if parsed is None:
        return False
    return parsed < MIN_KNOWN_GOOD_YTDLP


def _check_api_key(api_key: str, session=None):
    """Validate the key with one real call. Returns (name, ok, detail).

    Deliberately returns rather than raises: a bad key is a finding to
    report alongside the others, not a reason to abandon the diagnosis.
    """
    name = "YouTube Data API key works"
    try:
        details = youtube_api.fetch_video_details(
            [PROBE_TARGET_ID], api_key=api_key, session=session
        )
    except youtube_api.QuotaExceeded:
        return (
            name, False,
            "the key is valid but this project's 10,000 units/day are spent. "
            "Resets at midnight Pacific. Nothing is lost — queued work "
            "resumes then.",
        )
    except DetectorError as exc:
        return (
            name, False,
            "%s\n"
            "Common causes: the key is wrong or has a typo; 'YouTube Data "
            "API v3' is not enabled on the Cloud project; or the key's "
            "restrictions exclude that API." % str(exc)[:400],
        )

    found = details.get(PROBE_TARGET_ID)
    if found is None:
        return (
            name, False,
            "the call succeeded but returned no data for the test video, "
            "which should not happen — treat the key as unverified.",
        )

    # Deliberately NOT asserting a particular licence value. `status.license`
    # is a setting on someone else's upload and they can change it whenever
    # they like; asserting it would make this health check fail because of a
    # third party's edit, which says nothing about our own system. Verified
    # 2026-10-10: the test video's YouTube upload reads 'youtube' even though
    # the film itself is CC-BY — the field records what the uploader ticked,
    # not the work's actual licence. What we need to know is that the field
    # is readable and parses, which is what this asserts.
    known = {youtube_api.CREATIVE_COMMONS, youtube_api.STANDARD_LICENSE}
    if found.license not in known:
        return (
            name, False,
            "key accepted, but the licence field read %r, which is neither "
            "%r nor %r. The gate compares against those exact values, so an "
            "unrecognised one would be treated as restricted."
            % (found.license, youtube_api.CREATIVE_COMMONS,
               youtube_api.STANDARD_LICENSE),
        )
    return (
        name, True,
        "key accepted; Data API enabled; licence field readable "
        "(test video reads %r, duration %ss). 1 quota unit spent."
        % (found.license, found.duration_seconds),
    )


def run_diagnostics(
    api_key: Optional[str] = None,
    do_download: bool = True,
    dest_dir: Optional[Path] = None,
    player_clients=downloader.DEFAULT_PLAYER_CLIENTS,
    ydl_factory=None,
    session=None,
) -> Report:
    """Check everything the download stage needs, in dependency order."""
    report = Report()

    # 1. yt-dlp present, and new enough to work at all.
    try:
        import yt_dlp

        version = getattr(yt_dlp.version, "__version__", "unknown")
    except ImportError:
        report.add(
            "yt-dlp installed", False,
            "missing — pip install -r requirements.txt",
        )
        return report

    too_old = _ytdlp_is_too_old(version)
    report.add(
        "yt-dlp new enough",
        not too_old,
        "version %s" % version if not too_old else
        "version %s is older than %s, which is the oldest version measured to "
        "fetch YouTube media successfully. An out-of-date yt-dlp fails with "
        "403s and 'The page needs to be reloaded', which looks exactly like "
        "being blocked — so fix this before investigating anything else.\n"
        "  pip install --upgrade yt-dlp\n"
        "If your package index is stale and offers nothing newer:\n"
        "  pip install --upgrade 'yt-dlp @ git+https://github.com/yt-dlp/yt-dlp.git'"
        % (version, MIN_KNOWN_GOOD_YTDLP_STR),
    )

    # 2. FFmpeg, without which only 360p progressive is reachable.
    ffmpeg = shutil.which("ffmpeg")
    report.add(
        "FFmpeg on PATH",
        ffmpeg is not None,
        ffmpeg or "missing — macOS: brew install ffmpeg / Ubuntu: apt-get install ffmpeg",
    )

    # 3. The API key. Presence is not the question — whether it WORKS is.
    #    Checked against a video whose licence is known, so one call proves
    #    four things at once: the key is accepted, the Data API is enabled on
    #    the project, the quota is not spent, and `status.license` comes back
    #    with the exact value the download gate requires. Cost: 1 quota unit.
    if not api_key:
        report.add(
            "YouTube Data API key works",
            False,
            "no key set — the per-video licence check cannot run, so the gate "
            "will defer every video and nothing will ever download. "
            "Set YOUTUBE_API_KEY in .env (local) or as a GitHub Actions "
            "secret (CI); see .env.example for the setup steps.",
        )
    else:
        report.add(*_check_api_key(api_key, session=session))

    # 4. Can we read metadata at all?
    probe_result = None
    try:
        probe_result = downloader.probe(
            PROBE_TARGET, player_clients=player_clients, ydl_factory=ydl_factory
        )
        report.add(
            "probe a public video",
            True,
            "ok via the %r client (%s)" % (probe_result.player_client, PROBE_TARGET_NOTE),
        )
    except Exception as exc:
        report.add("probe a public video", False, str(exc)[:500])
        return report

    # 5. The one that actually matters: can bytes be fetched from here?
    if not do_download:
        report.add("fetch media bytes", True, "skipped (--no-download)")
        return report

    dest = Path(dest_dir) if dest_dir else Path(".")
    try:
        result = downloader.download(
            PROBE_TARGET,
            dest_dir=dest,
            preferred_max_height=144,
            max_bytes=20 * 1024 * 1024,
            player_clients=player_clients,
            allow_progressive_only=ffmpeg is None,
            ydl_factory=ydl_factory,
        )
        report.add(
            "fetch media bytes",
            True,
            "ok via %r, %.1f MB" % (result.player_client,
                                    result.size_bytes / (1024 * 1024)),
        )
    except Exception as exc:
        report.add(
            "fetch media bytes",
            False,
            "%s\n\nThis is the check that decides whether the pipeline can run "
            "unattended. If it fails here but passes on a GitHub runner, run "
            "the download stage only in CI. If it fails in CI too, see "
            "docs/LIMITS.md section 7." % str(exc)[:900],
        )
    return report


def format_report(report: Report) -> str:
    lines = ["DOWNLOAD ENVIRONMENT CHECK", "-" * 26]
    for check in report.checks:
        mark = "PASS" if check.ok else "FAIL"
        lines.append("[%s] %s" % (mark, check.name))
        if check.detail:
            for piece in check.detail.splitlines():
                lines.append("       %s" % piece)
    lines.append("")
    lines.append("Overall: %s" % ("ready" if report.ok else "NOT ready"))
    return "\n".join(lines)
