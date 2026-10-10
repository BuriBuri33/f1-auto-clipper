"""Command-line entry point.

Setup and diagnostics:

    python -m src.main init-db
    python -m src.main doctor                     # can this machine download?
    python -m src.main resolve-channel URL_OR_HANDLE

Pipeline, in order:

    python -m src.main check VIDEO [VIDEO ...]    # gate only, no download
    python -m src.main monitor  [--dry-run]
    python -m src.main download [--dry-run] [--keep] [--work-dir PATH]
    python -m src.main ingest   [--keep] [--work-dir PATH]   # download+transcribe
    python -m src.main analyze  [--dry-run]                   # score into clip candidates

Inspection and manual override (spec section 29):

    python -m src.main status
    python -m src.main sources
    python -m src.main source-enable NAME / source-disable NAME
    python -m src.main retry  [VIDEO_ID] [--reset-count]
    python -m src.main unskip VIDEO_ID

Phases 1-2 implement discovery and download. Transcription, analysis,
rendering and publishing arrive in later phases; their subcommands are not
registered until they exist, so `--help` never advertises something that
does not run.

Exit codes: 0 ok, 2 configuration error, 3 partial failure.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from .database import db
from .download import runner as download_runner
from .download import workspace
from .analysis import runner as analysis_runner
from .transcription import runner as ingest_runner
from .monitoring import source_monitor, youtube_rss
from .utils import logging as log_utils
from .utils.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SOURCES_PATH,
    ConfigError,
    load_config,
    load_dotenv,
    load_sources,
)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_PARTIAL_FAILURE = 3


def _emit_summary(text: str) -> None:
    """Mirror a report into the GitHub Actions run summary when running in CI.

    Spec section 31 asks for status via the Actions summary rather than a
    separate web service, which keeps the recurring cost at zero.
    """
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("```text\n%s\n```\n" % text)
    except OSError as exc:  # a broken summary file must not fail the run
        print("warning: could not write step summary: %s" % exc, file=sys.stderr)


def cmd_init_db(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    conn = db.connect(config.database_path)
    db.init_db(conn)
    sources = load_sources(args.sources)
    db.sync_sources(conn, sources)
    print("database ready at %s (schema v%d, %d source(s) synced)" % (
        config.database_path, db.SCHEMA_VERSION, len(sources)
    ))
    return EXIT_OK


def cmd_monitor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    sources = load_sources(args.sources)
    log_utils.configure(config.log_level, config.log_json)
    context = log_utils.LogContext(job_id=log_utils.new_job_id("monitor"))
    log = log_utils.get_logger("monitor", context)

    enabled = [s for s in sources if s.enabled]
    if not enabled:
        log.warning(
            "no enabled sources in %s — nothing to check" % (args.sources or DEFAULT_SOURCES_PATH)
        )
        return EXIT_OK

    if args.dry_run:
        print("DRY RUN — would check %d source(s):" % len(enabled))
        for s in enabled:
            print("  %-28s %s  rights_cleared=%s" % (s.name, s.channel_id, s.rights_cleared))
        return EXIT_OK

    conn = db.connect(config.database_path)
    db.init_db(conn)
    result = source_monitor.run_monitor(conn, config, sources, log=log)

    lines = ["F1 AUTO-CLIPPER — MONITOR", "-" * 26]
    for r in result.sources:
        if r.error:
            lines.append("  %-28s ERROR  %s" % (r.source, r.error))
        elif r.unchanged:
            lines.append("  %-28s unchanged" % r.source)
        else:
            lines.append(
                "  %-28s %d entries, %d new, %d skipped"
                % (r.source, r.entries_seen, len(r.new_videos), r.skipped)
            )
    lines.append("")
    lines.append("New videos this pass: %d" % result.new_count)
    if result.error_count:
        lines.append("Sources with errors:  %d" % result.error_count)
    _emit_summary("\n".join(lines))

    # A pass where every source failed is a failure; a pass where some
    # succeeded is reported but does not fail the workflow, so one dead
    # channel cannot stop the rest of the pipeline.
    if result.error_count and result.checked_count == 0:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def cmd_download(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    log_utils.configure(config.log_level, config.log_json)
    context = log_utils.LogContext(job_id=log_utils.new_job_id("download"))
    log = log_utils.get_logger("download", context)

    conn = db.connect(config.database_path)
    db.init_db(conn)

    dl_cfg = config.section("download")
    if args.dry_run:
        pending = db.videos_pending_download(
            conn,
            limit=int(dl_cfg.get("max_videos_per_run", 3)),
            max_retries=int(dl_cfg.get("max_retries", 3)),
        )
        if not pending:
            print("DRY RUN — nothing pending download")
            return EXIT_OK
        print("DRY RUN — %d candidate(s) pending download:" % len(pending))
        for row in pending:
            print(
                "  %-14s %-8s rights=%s retries=%d  %s"
                % (
                    row["youtube_video_id"],
                    row["status"],
                    "yes" if row["rights_cleared"] else "NO",
                    row["retry_count"],
                    row["title"][:60],
                )
            )
        if not downloader_ffmpeg_ok():
            print("\nNOTE: FFmpeg is not on PATH — a real run would refuse to "
                  "download. See `download.allow_progressive_only` in config.yaml.")
        return EXIT_OK

    configured_dir = args.work_dir or dl_cfg.get("work_dir") or ""
    with workspace.work_dir(configured_dir, keep=args.keep) as dest:
        log.info("working directory", fields={"path": dest})
        result = download_runner.run_download(conn, config, dest_dir=dest, log=log)

        lines = ["F1 AUTO-CLIPPER — DOWNLOAD", "-" * 26]
        if not result.outcomes:
            lines.append("  nothing pending")
        for outcome in result.outcomes:
            detail = outcome.reason
            if outcome.status == db.VideoStatus.DOWNLOADED.value:
                detail = "%.1f MB  %s" % (
                    (outcome.size_bytes or 0) / (1024 * 1024), outcome.path
                )
            lines.append("  %-14s %-11s %s" % (outcome.video_id, outcome.status, detail))
        lines.append("")
        lines.append("Considered: %d   downloaded: %d   skipped: %d   failed: %d"
                     % (result.considered, len(result.downloaded),
                        len(result.skipped), len(result.failed)))
        lines.append("Data API units this pass: %d" % result.api_units_spent)
        _emit_summary("\n".join(lines))

        if result.failed and not result.downloaded:
            return EXIT_PARTIAL_FAILURE
    return EXIT_OK


_VIDEO_ID_IN_URL = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def _as_video_id(token: str) -> Optional[str]:
    """Accept a bare id, a watch URL, a youtu.be link or a Shorts URL."""
    match = _VIDEO_ID_IN_URL.search(token)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", token):
        return token
    return None


def cmd_check(args: argparse.Namespace) -> int:
    """Run the pre-download gate against specific videos, downloading nothing.

    The cheapest way to find out whether a source is usable before committing
    to it: one quota unit covers up to 50 videos, and it reports the licence,
    duration and live status the gate decided on, plus the verdict and the
    reason. No media is fetched.
    """
    from .download.gate import GateConfig, evaluate
    from .download.runner import gate_config_from
    from .monitoring import youtube_api
    from .monitoring.types import DetectorError

    config = load_config(args.config)
    api_key = os.environ.get("YOUTUBE_API_KEY") or None
    if not api_key:
        print(
            "YOUTUBE_API_KEY is not set, so licence cannot be checked. Put it "
            "in .env (see .env.example).",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    ids, bad = [], []
    for token in args.videos:
        video_id = _as_video_id(token)
        (ids if video_id else bad).append(video_id or token)
    for token in bad:
        print("not a video id or URL: %r" % token, file=sys.stderr)
    if not ids:
        return EXIT_CONFIG_ERROR

    try:
        details = youtube_api.fetch_video_details(ids, api_key=api_key)
    except DetectorError as exc:
        print("could not look up videos: %s" % exc, file=sys.stderr)
        return EXIT_PARTIAL_FAILURE

    gate_cfg = gate_config_from(config)
    if args.assume_uncleared:
        gate_cfg = GateConfig(**dict(gate_cfg.__dict__))

    lines = ["PRE-DOWNLOAD GATE CHECK", "-" * 23,
             "Assuming the source IS rights-cleared: %s"
             % ("no" if args.assume_uncleared else "yes"), ""]
    for video_id in ids:
        found = details.get(video_id)
        decision = evaluate(not args.assume_uncleared, found, gate_cfg)
        lines.append("%s  %s" % (video_id, decision.verdict.value))
        if found is not None:
            lines.append("    title    %s" % found.title[:66])
            lines.append("    licence  %s%s" % (
                found.license,
                "  <- reusable" if found.is_creative_commons else "  <- NOT reusable",
            ))
            lines.append("    duration %ss" % found.duration_seconds)
            lines.append("    live     %s" % found.live_broadcast_content)
        else:
            lines.append("    not returned by the API (deleted, private or "
                         "region-blocked)")
        if decision.reason:
            lines.append("    reason   %s" % decision.reason)
        lines.append("")
    lines.append("1 quota unit spent (videos.list covers up to 50 ids per call).")
    lines.append("")
    lines.append(
        "A 'creativeCommon' licence is the UPLOADER's tickbox, not proof they "
        "held the right to apply it. See docs/RIGHTS.md."
    )
    _emit_summary("\n".join(lines))
    return EXIT_OK


def cmd_analyze(args: argparse.Namespace) -> int:
    """Score transcribed videos into ranked, deduplicated clip candidates."""
    config = load_config(args.config)
    log_utils.configure(config.log_level, config.log_json)
    context = log_utils.LogContext(job_id=log_utils.new_job_id("analyze"))
    log = log_utils.get_logger("analyze", context)

    conn = db.connect(config.database_path)
    db.init_db(conn)

    section = config.section("analysis")
    if args.dry_run:
        pending = db.videos_pending_analysis(
            conn, limit=int(section.get("max_videos_per_run", 3)),
            max_retries=int(section.get("max_retries", 3)),
        )
        if not pending:
            print("DRY RUN — nothing pending analysis")
            return EXIT_OK
        print("DRY RUN — %d video(s) pending analysis:" % len(pending))
        for row in pending:
            print("  %-14s %-8s  %s" % (row["youtube_video_id"], row["status"],
                                        row["title"][:60]))
        return EXIT_OK

    result = analysis_runner.run_analysis(conn, config, log=log)

    lines = ["F1 AUTO-CLIPPER — ANALYZE", "-" * 26]
    if not result.outcomes:
        lines.append("  nothing pending")
    for outcome in result.outcomes:
        detail = outcome.reason
        if outcome.status == db.VideoStatus.ANALYZED.value:
            detail = "%d candidate(s) via %s" % (outcome.candidates, outcome.method)
        lines.append("  %-14s %-12s %s" % (outcome.video_id, outcome.status, detail[:60]))
    lines.append("")
    lines.append("Analyzed: %d   failed: %d" % (result.analyzed, len(result.failed)))
    _emit_summary("\n".join(lines))

    if result.failed and not result.analyzed:
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def cmd_retry(args: argparse.Namespace) -> int:
    """Return failed videos to the queue (spec section 29)."""
    config = load_config(args.config)
    conn = db.connect(config.database_path)
    db.init_db(conn)

    revived = db.retry_failed(
        conn, video_id=args.video_id, reset_retry_count=args.reset_count
    )
    if not revived:
        target = args.video_id or "any video"
        print("nothing to retry (%s is not in FAILED state)" % target)
        return EXIT_OK
    print("requeued %d video(s): %s" % (len(revived), ", ".join(revived)))
    if not args.reset_count:
        print(
            "retry_count was left as-is, so a video at the ceiling will not be "
            "picked up. Pass --reset-count to clear it."
        )
    return EXIT_OK


def cmd_unskip(args: argparse.Namespace) -> int:
    """Give one skipped video another trip through the gate (spec section 29)."""
    config = load_config(args.config)
    conn = db.connect(config.database_path)
    db.init_db(conn)

    if not db.unskip(conn, args.video_id):
        print(
            "no SKIPPED video with id %r" % args.video_id, file=sys.stderr
        )
        return EXIT_CONFIG_ERROR
    print(
        "%s returned to DISCOVERED. The rights and licence gate runs again on "
        "the next download pass — this does not override it." % args.video_id
    )
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check whether this environment can actually download."""
    from .download import diagnose

    config = load_config(args.config)
    log_utils.configure(config.log_level, config.log_json)
    with workspace.work_dir(None, keep=False) as dest:
        report = diagnose.run_diagnostics(
            api_key=os.environ.get("YOUTUBE_API_KEY") or None,
            transcription_api_key=os.environ.get("TRANSCRIPTION_API_KEY") or None,
            llm_api_key=os.environ.get("LLM_API_KEY") or None,
            do_download=not args.no_download,
            dest_dir=dest,
        )
    _emit_summary(diagnose.format_report(report))
    return EXIT_OK if report.ok else EXIT_PARTIAL_FAILURE


def downloader_ffmpeg_ok() -> bool:
    from .download import downloader

    return downloader.ffmpeg_available()


def cmd_ingest(args: argparse.Namespace) -> int:
    """Download and transcribe in one pass.

    These are one command because the media file only exists inside a single
    run: it is too large to commit and the runner is destroyed afterwards.
    Transcribing in a later run would mean downloading the video twice.
    """
    config = load_config(args.config)
    log_utils.configure(config.log_level, config.log_json)
    context = log_utils.LogContext(job_id=log_utils.new_job_id("ingest"))
    log = log_utils.get_logger("ingest", context)

    conn = db.connect(config.database_path)
    db.init_db(conn)

    dl_cfg = config.section("download")
    configured_dir = args.work_dir or dl_cfg.get("work_dir") or ""
    with workspace.work_dir(configured_dir, keep=args.keep) as dest:
        log.info("working directory", fields={"path": dest})
        result = ingest_runner.run_ingest(conn, config, dest_dir=dest, log=log)

        lines = ["F1 AUTO-CLIPPER — INGEST", "-" * 24]
        if not result.outcomes:
            lines.append("  nothing pending")
        for outcome in result.outcomes:
            detail = outcome.reason
            if outcome.status == db.VideoStatus.TRANSCRIBED.value:
                detail = "%d segments via %s" % (outcome.segments or 0,
                                                 outcome.backend or "?")
            lines.append("  %-14s %-12s %s" % (outcome.video_id, outcome.status,
                                               detail[:60]))
        lines.append("")
        lines.append("Downloaded: %d   transcribed: %d   failed: %d"
                     % (result.downloaded, result.transcribed, len(result.failed)))
        lines.append("Data API units this pass: %d" % result.api_units_spent)
        lines.append("")
        lines.append("Media is discarded with the working directory; the "
                     "transcript is what persists.")
        _emit_summary("\n".join(lines))

        if result.failed and not result.transcribed:
            return EXIT_PARTIAL_FAILURE
    return EXIT_OK


def cmd_prune_transcripts(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    conn = db.connect(config.database_path)
    db.init_db(conn)
    removed = ingest_runner.prune_transcripts(conn)
    if not removed:
        print("no transcripts eligible for pruning "
              "(only PUBLISHED and SKIPPED videos qualify)")
        return EXIT_OK
    print("pruned %d transcript(s): %s" % (len(removed), ", ".join(removed)))
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Pipeline status, in the shape spec section 31 illustrates.

    Deliberately printed to stdout AND appended to the GitHub Actions run
    summary, so the same command serves a terminal and a workflow page and
    there is no separate web service to host.
    """
    config = load_config(args.config)
    conn = db.connect(config.database_path)
    db.init_db(conn)

    sources = db.list_sources(conn)
    counts = db.status_counts(conn)
    clip_counts = db.count_clips_by_status(conn)
    last = db.last_check_time(conn) or "never"

    lines = [
        "F1 AUTO-CLIPPER",
        "-" * 16,
        "Last source check: %s" % last,
        "New videos:        %d  (discovered in the last 24h)" % db.count_new_since(conn, 24),
        "Processing:        %d" % db.count_in_progress(conn),
        "Candidates:        %d" % clip_counts.get(db.ClipStatus.CANDIDATE.value, 0),
        "Today's clip:      %s" % _todays_clip_state(),
        "YouTube:           %s" % _platform_state("youtube", config),
        "Instagram:         %s" % _platform_state("instagram", config),
        "TikTok:            %s" % _platform_state("tiktok", config),
        "",
        "Sources:           %d enabled / %d total"
        % (sum(1 for r in sources if r["enabled"]), len(sources)),
        "Rights-cleared:    %d" % sum(1 for r in sources if r["rights_cleared"]),
        "Pending download:  %d" % len(db.videos_pending_download(conn, limit=1000)),
        "Pending transcribe: %d" % len(db.videos_pending_transcription(conn, limit=1000)),
        "Pending analysis:   %d" % len(db.videos_pending_analysis(conn, limit=1000)),
        "FFmpeg available:  %s" % ("yes" if downloader_ffmpeg_ok() else "NO"),
        "Data API units:    %d / 10000 today"
        % db.api_usage_today(conn, "youtube_data_api"),
        "",
        "Source videos by status:",
    ]
    if counts:
        for status in db.VideoStatus:
            n = counts.get(status.value, 0)
            if n:
                lines.append("  %-12s %d" % (status.value, n))
    else:
        lines.append("  (none yet)")

    _emit_summary("\n".join(lines))
    return EXIT_OK


def _todays_clip_state() -> str:
    """Honest placeholder until the render stage exists (Phase 5)."""
    return "n/a (rendering not implemented — Phase 5)"


def _platform_state(platform: str, config) -> str:
    """Whether a publisher is enabled in config, and whether it exists yet.

    Reports configuration, not capability, and says so: a line reading
    "PUBLISHED" for a publisher that has not been written would be a lie in
    the one output the operator relies on.
    """
    publishing = config.section("publishing")
    phase = {"youtube": 6, "instagram": 7, "tiktok": 8}[platform]
    if not publishing.get(platform, False):
        return "disabled in config"
    return "enabled in config, not implemented (Phase %d)" % phase


def cmd_sources(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    conn = db.connect(config.database_path)
    db.init_db(conn)
    rows = db.list_sources(conn)
    if not rows:
        print("no sources in the database yet — run: python -m src.main init-db")
        return EXIT_OK
    print("%-28s %-26s %-8s %-8s %s" % ("NAME", "CHANNEL_ID", "ENABLED", "RIGHTS", "LAST CHECK"))
    for row in rows:
        print(
            "%-28s %-26s %-8s %-8s %s"
            % (
                row["name"],
                row["channel_id"],
                "yes" if row["enabled"] else "no",
                "yes" if row["rights_cleared"] else "NO",
                row["last_checked_at"] or "never",
            )
        )
    return EXIT_OK


def _set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    config = load_config(args.config)
    conn = db.connect(config.database_path)
    db.init_db(conn)
    if not db.set_source_enabled(conn, args.name, enabled):
        print("no source named %r" % args.name, file=sys.stderr)
        return EXIT_CONFIG_ERROR
    print(
        "%s %r in the database. Note: the next `monitor` run re-applies "
        "config/sources.yaml, so edit the YAML to make this permanent."
        % ("enabled" if enabled else "disabled", args.name)
    )
    return EXIT_OK


# Order matters. A channel page embeds the ids of every channel it links to
# — featured channels, the uploader of each shelf video — and the FIRST
# "channelId" in the HTML is routinely one of those, not the page's own. The
# canonical link and og:url are the only places that always name the page
# itself. Observed on 2026-10-10: the first "channelId" match for
# youtube.com/@Formula1 was an unrelated channel, which would have silently
# pointed a source at the wrong feed.
_CHANNEL_ID_PATTERNS = (
    re.compile(r'rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'property="og:url"\s+content="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"'),
    re.compile(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"'),
)
_CHANNEL_ID_IN_URL = re.compile(r"/channel/(UC[A-Za-z0-9_-]{22})")


def resolve_channel_id(html: str) -> Optional[str]:
    """Extract the page's OWN channel id from a channel page's HTML."""
    for pattern in _CHANNEL_ID_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def cmd_resolve_channel(args: argparse.Namespace) -> int:
    """Find the UC... channel id behind a channel URL or @handle."""
    import requests

    target = args.url
    if not target.startswith("http"):
        target = "https://www.youtube.com/%s" % target.lstrip("/")

    # A /channel/UC... URL already carries the answer; no request needed.
    direct = _CHANNEL_ID_IN_URL.search(target)
    if direct:
        channel_id = direct.group(1)
        print(channel_id)
        return EXIT_OK

    try:
        response = requests.get(
            target,
            headers={"User-Agent": "Mozilla/5.0 (compatible; f1-auto-clipper/0.1)"},
            timeout=20,
        )
        response.raise_for_status()
    except Exception as exc:
        print("could not fetch %s: %s" % (target, exc), file=sys.stderr)
        return EXIT_CONFIG_ERROR

    channel_id = resolve_channel_id(response.text)
    if not channel_id:
        print(
            "no channel id found in the page. Open the channel, View Source, "
            "and search for 'rel=\"canonical\"' — do NOT just take the first "
            "\"channelId\" you see, it is often a different channel.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR
    print(channel_id)
    print(
        "\nAdd to config/sources.yaml (and set rights_cleared only if you "
        "hold the rights — see docs/RIGHTS.md):\n"
        "  - name: CHANGE_ME\n"
        "    channel_id: \"%s\"\n"
        "    enabled: true\n"
        "    rights_cleared: false\n"
        "    rights_note: \"\"" % channel_id,
        file=sys.stderr,
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="f1-auto-clipper", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.yaml (default: %s)" % DEFAULT_CONFIG_PATH)
    parser.add_argument("--sources", type=Path, default=None,
                        help="path to sources.yaml (default: %s)" % DEFAULT_SOURCES_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the database and sync sources").set_defaults(
        func=cmd_init_db
    )

    p_monitor = sub.add_parser("monitor", help="check every enabled source for new uploads")
    p_monitor.add_argument("--dry-run", action="store_true",
                           help="list what would be checked, make no requests")
    p_monitor.set_defaults(func=cmd_monitor)

    p_dl = sub.add_parser("download", help="download videos pending download")
    p_dl.add_argument("--dry-run", action="store_true",
                      help="list candidates without fetching anything")
    p_dl.add_argument("--work-dir", default=None,
                      help="where to put downloads (overrides config)")
    p_dl.add_argument("--keep", action="store_true",
                      help="do not delete the working directory on exit")
    p_dl.set_defaults(func=cmd_download)

    p_ing = sub.add_parser(
        "ingest", help="download AND transcribe in one pass (what CI runs)"
    )
    p_ing.add_argument("--work-dir", default=None,
                       help="where to put media (overrides config)")
    p_ing.add_argument("--keep", action="store_true",
                       help="do not delete the working directory on exit")
    p_ing.set_defaults(func=cmd_ingest)

    sub.add_parser(
        "prune-transcripts",
        help="delete transcripts for PUBLISHED/SKIPPED videos",
    ).set_defaults(func=cmd_prune_transcripts)

    p_an = sub.add_parser("analyze", help="score transcribed videos into ranked clip candidates")
    p_an.add_argument("--dry-run", action="store_true",
                      help="list videos pending analysis without scoring")
    p_an.set_defaults(func=cmd_analyze)

    p_check = sub.add_parser(
        "check",
        help="run the pre-download gate against video ids/URLs, downloading nothing",
    )
    p_check.add_argument("videos", nargs="+", metavar="VIDEO",
                         help="video id, watch URL, youtu.be link or Shorts URL")
    p_check.add_argument("--assume-uncleared", action="store_true",
                         help="evaluate as if the source were NOT rights-cleared")
    p_check.set_defaults(func=cmd_check)

    p_retry = sub.add_parser("retry", help="requeue failed videos (manual override)")
    p_retry.add_argument("video_id", nargs="?", default=None,
                         help="one video id; omit for every FAILED video")
    p_retry.add_argument("--reset-count", action="store_true",
                         help="also clear retry_count, reviving a video at the ceiling")
    p_retry.set_defaults(func=cmd_retry)

    p_unskip = sub.add_parser(
        "unskip", help="return one SKIPPED video to the queue (manual override)"
    )
    p_unskip.add_argument("video_id")
    p_unskip.set_defaults(func=cmd_unskip)

    p_doc = sub.add_parser(
        "doctor", help="check whether this environment can download at all"
    )
    p_doc.add_argument("--no-download", action="store_true",
                       help="check metadata only; do not fetch media bytes")
    p_doc.set_defaults(func=cmd_doctor)

    sub.add_parser("status", help="print a pipeline status report").set_defaults(
        func=cmd_status
    )
    sub.add_parser("sources", help="list known sources").set_defaults(func=cmd_sources)

    p_en = sub.add_parser("source-enable", help="enable a source (manual override)")
    p_en.add_argument("name")
    p_en.set_defaults(func=lambda a: _set_enabled(a, True))

    p_dis = sub.add_parser("source-disable", help="disable a source (manual override)")
    p_dis.add_argument("name")
    p_dis.set_defaults(func=lambda a: _set_enabled(a, False))

    p_res = sub.add_parser("resolve-channel", help="find the UC... id for a channel URL/@handle")
    p_res.add_argument("url")
    p_res.set_defaults(func=cmd_resolve_channel)

    return parser


def _shield_leading_dash_ids(argv: List[str]) -> List[str]:
    """Insert `--` after `check` so video ids starting with `-` survive argparse.

    YouTube ids are 11 characters of [A-Za-z0-9_-], so roughly one in sixteen
    begins with a hyphen, and argparse reads that as an option flag. Telling
    people to type `--` first is a papercut they would hit constantly, so the
    shim is here instead. Only `check` takes free-form ids, and only the first
    `--` is added — an explicit one is left alone.
    """
    if "check" not in argv:
        return argv
    index = argv.index("check")
    rest = argv[index + 1:]
    if "--" in rest or not rest:
        return argv
    # Leave real options for `check` ahead of the ids, so
    # `check --assume-uncleared ID` still works.
    first_id = 0
    while first_id < len(rest) and rest[first_id].startswith("--"):
        first_id += 1
    return argv[: index + 1] + rest[:first_id] + ["--"] + rest[first_id:]


def main(argv: Optional[List[str]] = None) -> int:
    # Before anything reads os.environ. A real environment variable always
    # wins over the file, so this is a local-development convenience and can
    # never shadow a CI secret.
    load_dotenv()
    argv = _shield_leading_dash_ids(list(argv if argv is not None else sys.argv[1:]))
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print("configuration error: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
