"""Environment self-test for the ingest pipeline.

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


def _check_transcription_key(api_key: Optional[str], ffmpeg, session=None):
    """Validate the transcription backend. Returns (name, ok, detail)."""
    name = "transcription backend"

    if not api_key:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return (
                name, False,
                "no TRANSCRIPTION_API_KEY and faster-whisper is not installed, "
                "so there is no way to transcribe at all.\n"
                "  Either: get a free key at https://console.groq.com/keys\n"
                "  Or:     pip install -r requirements-transcribe.txt",
            )
        return (
            name, True,
            "no TRANSCRIPTION_API_KEY — will use local faster-whisper. Works "
            "with no key and no quota, but runs at roughly real time, so a "
            "3-hour source is a 3-hour job.",
        )

    if not ffmpeg:
        return (name, False, "cannot test the key without FFmpeg to make a sample")

    # One second of silence is enough to prove the key, the endpoint and the
    # response shape. Groq bills a 10-second minimum per request, which is
    # negligible against ~28,800 audio seconds a day.
    import subprocess
    import tempfile

    from ..transcription import groq_backend
    from ..transcription.types import (
        TranscriptionError,
        TranscriptionQuotaExceeded,
    )

    with tempfile.TemporaryDirectory() as tmp:
        sample = Path(tmp) / "probe.mp3"
        made = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
             "-f", "lavfi", "-t", "1", "-i", "anullsrc=r=16000:cl=mono",
             "-ac", "1", "-ar", "16000", "-b:a", "32k", str(sample)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if made.returncode != 0 or not sample.exists():
            return (name, False, "could not build a sample clip with FFmpeg")

        transcriber = groq_backend.GroqTranscriber(
            api_key=api_key, session=session, max_retries=2, sleep=lambda _: None
        )
        try:
            payload = transcriber.transcribe_file(sample)
        except TranscriptionQuotaExceeded as exc:
            return (
                name, False,
                "the key is valid but Groq's free daily allowance is spent. "
                "Work stays queued and resumes after the reset; nothing is "
                "charged.\n  %s" % str(exc)[:200],
            )
        except TranscriptionError as exc:
            return (name, False, str(exc)[:400])

    if not isinstance(payload, dict) or "text" not in payload:
        return (
            name, False,
            "Groq accepted the key but the response was not the expected "
            "verbose_json shape — keys: %s"
            % sorted(payload)[:8] if isinstance(payload, dict) else type(payload),
        )
    return (
        name, True,
        "Groq key accepted, endpoint reachable, verbose_json parsed "
        "(model %s)" % transcriber.model,
    )


def _check_llm_key(api_key: Optional[str], session=None):
    """Validate the clip-selection LLM backend. Returns (name, ok, detail)."""
    name = "clip-selection LLM backend"

    if not api_key:
        return (
            name, True,
            "no LLM_API_KEY — will use the heuristic scorer. Always works, "
            "but ranks candidates on keyword/structure signals only, not "
            "semantic judgement. Get a free key at "
            "https://aistudio.google.com/apikey to use Gemini instead.",
        )

    from ..analysis.clip_selector import DEFAULT_MODEL, GeminiClipSelector
    from ..analysis.candidates import CandidateWindow
    from ..analysis.types import AnalysisError, AnalysisQuotaExceeded
    from ..transcription.types import Segment

    selector = GeminiClipSelector(
        api_key=api_key, session=session, max_retries=2, sleep=lambda _: None
    )
    probe_window = CandidateWindow([Segment(0.0, 20.0, "Box box box this lap.")])
    try:
        selector.score_windows([probe_window])
    except AnalysisQuotaExceeded as exc:
        return (
            name, False,
            "the key is valid but Gemini's free-tier allowance is spent. "
            "Work stays queued; nothing is lost and nothing is charged.\n"
            "  %s" % str(exc)[:200],
        )
    except AnalysisError as exc:
        return (name, False, str(exc)[:400])
    return (
        name, True,
        "Gemini key accepted, endpoint reachable, structured output parsed "
        "(model %s)" % selector.model,
    )


def _check_youtube_publish_credentials(
    client_id: Optional[str], client_secret: Optional[str],
    refresh_token: Optional[str], session=None,
):
    """Validate the Phase 6 OAuth credentials. Returns (name, ok, detail)."""
    name = "YouTube publish credentials"
    have_any = any((client_id, client_secret, refresh_token))
    have_all = all((client_id, client_secret, refresh_token))

    if not have_any:
        return (
            name, True,
            "no YOUTUBE_CLIENT_ID/_SECRET/_REFRESH_TOKEN — publishing to "
            "YouTube will be skipped. See README for the one-time OAuth "
            "setup to mint a refresh token.",
        )
    if not have_all:
        return (
            name, False,
            "only some of YOUTUBE_CLIENT_ID/_CLIENT_SECRET/_REFRESH_TOKEN "
            "are set — publishing needs all three or none.",
        )

    from ..publishing import youtube
    from ..publishing.types import PublishError

    try:
        youtube.refresh_access_token(client_id, client_secret, refresh_token, session=session)
    except PublishError as exc:
        return (name, False, str(exc)[:400])
    return (
        name, True,
        "refresh token accepted, access token minted. No quota spent — this "
        "is Google's OAuth endpoint, not the Data API.",
    )


def _check_instagram_credentials(
    access_token: Optional[str], ig_account_id: Optional[str], session=None,
):
    """Validate the Phase 7 Graph API credentials. Returns (name, ok, detail)."""
    name = "Instagram publish credentials"
    have_any = any((access_token, ig_account_id))
    have_all = all((access_token, ig_account_id))

    if not have_any:
        return (
            name, True,
            "no META_ACCESS_TOKEN/INSTAGRAM_ACCOUNT_ID — publishing to "
            "Instagram will be skipped. See README for the Meta app / "
            "Professional account setup.",
        )
    if not have_all:
        return (
            name, False,
            "only one of META_ACCESS_TOKEN/INSTAGRAM_ACCOUNT_ID is set — "
            "publishing needs both or neither.",
        )

    from ..publishing import instagram
    from ..publishing.types import PublishError

    try:
        info = instagram.get_account_info(ig_account_id, access_token, session=session)
    except PublishError as exc:
        return (name, False, str(exc)[:400])
    return (
        name, True,
        "access token accepted (account %s). Long-lived Meta tokens expire "
        "around 60 days and must be refreshed manually."
        % info.get("username", ig_account_id),
    )


def run_diagnostics(
    api_key: Optional[str] = None,
    transcription_api_key: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    youtube_client_id: Optional[str] = None,
    youtube_client_secret: Optional[str] = None,
    youtube_refresh_token: Optional[str] = None,
    instagram_access_token: Optional[str] = None,
    instagram_account_id: Optional[str] = None,
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

    # 2b. The render stage's filters specifically. Measured 2026-10-10: a
    #     plain Homebrew `ffmpeg` has neither `ass` nor `drawtext` — both
    #     need libass/libfreetype, which that formula does not bundle. A
    #     present `ffmpeg` binary proves nothing about caption/watermark
    #     support, so this checks the actual filter list. See LIMITS.md 8a.
    if ffmpeg:
        from ..video import ffmpeg as video_ffmpeg

        missing = video_ffmpeg.missing_required_filters()
        report.add(
            "FFmpeg has caption/watermark filters",
            not missing,
            "all present" if not missing else
            "missing: %s. Needed for Phase 5 rendering (burned-in captions "
            "use `ass`, the watermark uses `drawtext`). On Ubuntu/Debian "
            "the distro ffmpeg package normally has both; on macOS "
            "Homebrew's plain `ffmpeg` formula does not — use `ffmpeg-full` "
            "instead." % ", ".join(missing),
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

    # 4. The transcription key, if one is configured. Validated with a real
    #    call, because "present" says nothing: a wrong key, a disabled
    #    account and a spent allowance all look identical until you ask.
    report.add(*_check_transcription_key(transcription_api_key, ffmpeg, session))

    # 4b. The clip-selection LLM key, if one is configured — same reasoning:
    #    "present" says nothing about "accepted", and the model catalog
    #    moves fast enough that a working key can still 404 on a dead model
    #    name (verified: gemini-2.5-flash did exactly this on 2026-10-10).
    report.add(*_check_llm_key(llm_api_key, session))

    # 4c/4d. The publishing credentials, if configured — Phase 6/7. Same
    #    reasoning again: these are OAuth credentials that expire/rotate
    #    (a refresh token can be revoked, a long-lived Meta token lasts
    #    ~60 days), so "present" is not "working".
    report.add(*_check_youtube_publish_credentials(
        youtube_client_id, youtube_client_secret, youtube_refresh_token, session,
    ))
    report.add(*_check_instagram_credentials(
        instagram_access_token, instagram_account_id, session,
    ))

    # 5. Can we read metadata at all?
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
    lines = ["PIPELINE ENVIRONMENT CHECK", "-" * 26]
    for check in report.checks:
        mark = "PASS" if check.ok else "FAIL"
        lines.append("[%s] %s" % (mark, check.name))
        if check.detail:
            for piece in check.detail.splitlines():
                lines.append("       %s" % piece)
    lines.append("")
    lines.append("Overall: %s" % ("ready" if report.ok else "NOT ready"))
    return "\n".join(lines)
