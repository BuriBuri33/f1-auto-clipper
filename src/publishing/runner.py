"""Orchestrates one publish pass: render, then push the result to every
enabled, configured platform (spec sections 19-20).

**Why this wraps render rather than running after it.** Nothing persists the
rendered file between workflow runs (spec section 5's deviation note; Phase 5
re-downloads the source for exactly this reason). A clip that rendered
successfully in one run and failed to upload cannot be "retried" by
publishing alone in a later run — there would be no file left to upload. So
publishing happens in the SAME process, immediately after render, while the
file still exists on disk. This is the same reasoning that welded download
to transcription in `ingest`, one phase later in the pipeline.

Platform independence: a clip can succeed on YouTube and fail on Instagram
(or vice versa) in the same run. Each platform's outcome is its own
``publications`` row; the clip is marked PUBLISHED as soon as ANY enabled
platform succeeds, not only when all of them do — a clip live on one
platform is a real, useful outcome, not a partial failure. A video with zero
platform successes goes to FAILED and retries the whole render+publish pass
next time; **TikTok (Phase 8) is not implemented and is always skipped here**
regardless of ``publishing.tiktok`` in config, same as `status` already
reports it.

**Phase 9: the 1/day cap and cross-video selection are real, enforced
here.** `db.clips_published_today` is checked against
``publishing.max_per_day`` before anything is rendered or downloaded — once
the day's quota is spent, the run stops cleanly with
``PublishRunResult.daily_cap_reached`` set, rather than rendering a clip
that would only be discarded. Below the cap, `db.videos_ready_for_publish`
selects ANALYZED videos ordered by their own best CANDIDATE clip's score, so
the clip spent on each of the day's slots is the best one available across
every video, not just the best one in whichever video sorts first by
recency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..database import db
from ..utils.config import Config
from ..utils.logging import ContextLogger, get_logger
from ..video import ffmpeg as video_ffmpeg
from ..video import runner as render_runner
from . import instagram, youtube
from .hosting import DEFAULT_RELEASE_TAG
from .types import PublishError

# TikTok (Phase 8) is deliberately out of scope here — see module docstring.
IMPLEMENTED_PLATFORMS = ("youtube", "instagram")


@dataclass
class PlatformOutcome:
    platform: str
    status: str                  # PublicationStatus value
    detail: str = ""
    post_id: Optional[str] = None


@dataclass
class VideoPublishOutcome:
    video_id: str
    title: str
    status: str                  # VideoStatus value
    reason: str = ""
    clip_id: Optional[int] = None
    platforms: List[PlatformOutcome] = field(default_factory=list)


@dataclass
class PublishRunResult:
    outcomes: List[VideoPublishOutcome] = field(default_factory=list)
    environment_error: Optional[str] = None
    daily_cap_reached: bool = False
    published_today: int = 0
    max_per_day: int = 1

    @property
    def published(self) -> int:
        return sum(1 for o in self.outcomes if o.status == db.VideoStatus.PUBLISHED.value)

    @property
    def failed(self) -> List[VideoPublishOutcome]:
        return [o for o in self.outcomes if o.status == db.VideoStatus.FAILED.value]


@dataclass(frozen=True)
class YouTubeCredentials:
    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True)
class InstagramCredentials:
    access_token: str
    ig_account_id: str


def _github_repo() -> Optional[str]:
    """"owner/repo", from the Actions-provided environment variable.

    Not configurable in config.yaml: it must always be THIS repository (the
    one with 'contents: write' on its own GITHUB_TOKEN), so an operator
    cannot accidentally point hosting at a repo the token has no access to.
    """
    return os.environ.get("GITHUB_REPOSITORY") or None


def _publish_clip_to_platform(
    conn, platform: str, clip_row, video_row, config: Config, log: ContextLogger,
    youtube_creds: Optional[YouTubeCredentials],
    instagram_creds: Optional[InstagramCredentials],
    github_token: Optional[str],
    session=None,
) -> PlatformOutcome:
    clip_id = int(clip_row["id"])
    existing = db.publication_status(conn, clip_id, platform)
    if existing == db.PublicationStatus.PUBLISHED.value:
        return PlatformOutcome(platform, existing, "already published, skipped")

    pub_cfg = config.section("publishing")
    hashtags = [h for h in (clip_row["hashtags"] or "").split(",") if h]
    path = Path(clip_row["rendered_path"])

    try:
        if platform == "youtube":
            if youtube_creds is None:
                return PlatformOutcome(
                    platform, db.PublicationStatus.SKIPPED.value,
                    "YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN not set",
                )
            privacy = "private" if bool(pub_cfg.get("force_private", True)) else "public"

            def spend(units: int) -> None:
                db.record_api_usage(conn, "youtube_data_api", units)

            result = youtube.upload(
                path, clip_row["title"] or "F1 highlight clip",
                clip_row["description"] or "", hashtags,
                youtube_creds.client_id, youtube_creds.client_secret,
                youtube_creds.refresh_token, privacy_status=privacy,
                category_id=str(pub_cfg.get("youtube_category_id", "17")),
                session=session, on_quota_spend=spend,
            )
            return PlatformOutcome(
                platform, db.PublicationStatus.PUBLISHED.value, result.url, result.video_id,
            )

        if platform == "instagram":
            if instagram_creds is None:
                return PlatformOutcome(
                    platform, db.PublicationStatus.SKIPPED.value,
                    "META_ACCESS_TOKEN/INSTAGRAM_ACCOUNT_ID not set",
                )
            repo = _github_repo()
            if not repo or not github_token:
                return PlatformOutcome(
                    platform, db.PublicationStatus.SKIPPED.value,
                    "GITHUB_REPOSITORY/GITHUB_TOKEN not available — needed to "
                    "host the clip at a public URL for Instagram to fetch",
                )
            result = instagram.publish(
                path, clip_row["title"] or "", clip_row["description"] or "",
                hashtags, instagram_creds.ig_account_id, instagram_creds.access_token,
                repo, github_token, session=session,
                poll_interval=float(pub_cfg.get("instagram_poll_interval_seconds", 10)),
                poll_timeout=float(pub_cfg.get("instagram_poll_timeout_seconds", 600)),
                github_release_tag=str(pub_cfg.get("github_release_tag", DEFAULT_RELEASE_TAG)),
            )
            return PlatformOutcome(
                platform, db.PublicationStatus.PUBLISHED.value, "", result.media_id,
            )

        raise AssertionError("unreachable: unknown platform %r" % platform)

    except PublishError as exc:
        log.warning(
            "publish to %s failed" % platform,
            fields={"clip_id": clip_id, "error_type": type(exc).__name__, "error": str(exc)},
        )
        return PlatformOutcome(platform, db.PublicationStatus.FAILED.value, str(exc))


def publish_one_video(
    conn, row, config: Config, work_dir: Path, log: ContextLogger,
    youtube_creds: Optional[YouTubeCredentials] = None,
    instagram_creds: Optional[InstagramCredentials] = None,
    github_token: Optional[str] = None,
    ydl_factory=None,
    session=None,
) -> VideoPublishOutcome:
    render_outcome = render_runner.render_one_video(
        conn, row, config, work_dir, log, ydl_factory=ydl_factory,
    )
    if render_outcome.status != db.VideoStatus.RENDERED.value:
        return VideoPublishOutcome(
            render_outcome.video_id, render_outcome.title, render_outcome.status,
            render_outcome.reason,
        )

    clip_row = db.get_clip(conn, render_outcome.clip_id)
    vlog = log.bind(source_video=row["youtube_video_id"], clip_id=render_outcome.clip_id)

    pub_cfg = config.section("publishing")
    platform_outcomes: List[PlatformOutcome] = []
    for platform in IMPLEMENTED_PLATFORMS:
        if not bool(pub_cfg.get(platform, False)):
            platform_outcomes.append(
                PlatformOutcome(platform, db.PublicationStatus.SKIPPED.value, "disabled in config")
            )
            continue
        outcome = _publish_clip_to_platform(
            conn, platform, clip_row, row, config, vlog,
            youtube_creds, instagram_creds, github_token, session=session,
        )
        platform_outcomes.append(outcome)
        db.record_publication(
            conn, render_outcome.clip_id, platform,
            db.PublicationStatus(outcome.status), platform_post_id=outcome.post_id,
            error=outcome.detail if outcome.status == db.PublicationStatus.FAILED.value else None,
        )

    succeeded = [o for o in platform_outcomes if o.status == db.PublicationStatus.PUBLISHED.value]
    if succeeded:
        db.mark_clip_published(conn, render_outcome.clip_id)
        db.set_video_status(conn, row["id"], db.VideoStatus.PUBLISHED)
        vlog.info("published", fields={"platforms": [o.platform for o in succeeded]})
        return VideoPublishOutcome(
            render_outcome.video_id, render_outcome.title, db.VideoStatus.PUBLISHED.value,
            clip_id=render_outcome.clip_id, platforms=platform_outcomes,
        )

    reason = "rendered, but every enabled platform failed to publish"
    db.set_video_status(conn, row["id"], db.VideoStatus.FAILED, reason, bump_retry=True)
    vlog.warning("publish failed on every enabled platform",
                 fields={"platforms": [(o.platform, o.status) for o in platform_outcomes]})
    return VideoPublishOutcome(
        render_outcome.video_id, render_outcome.title, db.VideoStatus.FAILED.value, reason,
        clip_id=render_outcome.clip_id, platforms=platform_outcomes,
    )


def run_publish(
    conn, config: Config, work_dir: Path, log: Optional[ContextLogger] = None,
    youtube_creds: Optional[YouTubeCredentials] = None,
    instagram_creds: Optional[InstagramCredentials] = None,
    github_token: Optional[str] = None,
    ydl_factory=None,
    session=None,
) -> PublishRunResult:
    log = log or get_logger(__name__)
    pub_cfg = config.section("publishing")
    max_per_day = int(pub_cfg.get("max_per_day", 1))
    published_today = db.clips_published_today(conn)
    result = PublishRunResult(
        published_today=published_today, max_per_day=max_per_day,
        daily_cap_reached=published_today >= max_per_day,
    )

    missing = video_ffmpeg.missing_required_filters()
    if missing:
        result.environment_error = (
            "FFmpeg is missing required filter(s): %s — nothing was "
            "rendered or published. See docs/LIMITS.md section 8a."
            % ", ".join(missing)
        )
        log.error("publish environment check failed", fields={"missing_filters": missing})
        return result

    max_videos_per_run = int(pub_cfg.get("max_videos_per_run", 1))
    max_retries = int(pub_cfg.get("max_retries", 3))
    new_slots = max(0, max_per_day - published_today)

    # Ordered best-candidate-first across every video (spec section 12).
    # Not pre-trimmed to `new_slots`: a video whose best clip already has a
    # PUBLISHED platform today (a continuation — e.g. a crash between
    # platforms, or an earlier partial run) must still be considered even
    # once the day's new-clip quota is spent, since finishing it costs no
    # additional slot. See `clip_has_publication_today`.
    rows = db.videos_ready_for_publish(conn, limit=max_videos_per_run, max_retries=max_retries)

    considered = 0
    for row in rows:
        if considered >= max_videos_per_run:
            break
        candidates = db.candidate_clips_for_render(conn, row["id"])
        if not candidates:
            continue
        is_continuation = db.clip_has_publication_today(conn, candidates[0]["id"])
        if not is_continuation and new_slots <= 0:
            continue  # the day's quota is spent and this would be a new clip
        outcome = publish_one_video(
            conn, row, config, work_dir, log,
            youtube_creds=youtube_creds, instagram_creds=instagram_creds,
            github_token=github_token, ydl_factory=ydl_factory, session=session,
        )
        result.outcomes.append(outcome)
        considered += 1
        if not is_continuation:
            new_slots -= 1

    log.info(
        "publish pass complete",
        fields={"considered": considered, "published": result.published,
               "failed": len(result.failed), "published_today": published_today,
               "max_per_day": max_per_day},
    )
    return result
