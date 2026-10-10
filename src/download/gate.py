"""The pre-download gate.

Every reason a video must not be downloaded is decided here, before a single
media byte is fetched. Keeping the decision in one pure function — no network,
no database, no filesystem — is what makes it fully testable, and this is the
code most worth testing in the project.

Order matters. Rights come first, so a rights failure is the reason reported
even when the video is also too long or a livestream: the operator needs to
see the rights problem, not a length problem that happens to be checked
first.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..monitoring.youtube_api import CREATIVE_COMMONS, VideoDetails


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    SKIP = "SKIP"      # permanently ineligible — do not retry
    DEFER = "DEFER"    # cannot decide yet — retry later


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


@dataclass(frozen=True)
class GateConfig:
    """Thresholds the gate applies. Mirrors config.yaml."""

    require_creative_commons: bool = True
    max_source_duration_minutes: int = 180
    min_source_duration_seconds: int = 120
    max_download_size_mb: int = 2000


def evaluate(
    rights_cleared: bool,
    details: Optional[VideoDetails],
    config: GateConfig,
) -> GateDecision:
    """Decide whether one video may be downloaded.

    ``details`` is None when the Data API could not tell us about the video —
    deleted, private, region-blocked, or no API key configured. That is a
    DEFER, not a SKIP: the inability to verify is not evidence of
    ineligibility, and a video that is temporarily unavailable may be fine
    tomorrow. It is emphatically not an ALLOW either.
    """
    # 1. Rights, first and unconditionally.
    if not rights_cleared:
        return GateDecision(
            Verdict.SKIP, "source is not rights-cleared"
        )

    if details is None:
        return GateDecision(
            Verdict.DEFER,
            "could not verify the video with the Data API (no key, or the "
            "video is private, deleted or region-blocked) — not downloading "
            "something we cannot check",
        )

    # 2. Licence is a property of the VIDEO, not of the channel. A
    #    rights-cleared CC channel can still contain all-rights-reserved
    #    uploads, so the channel flag alone is never sufficient.
    if config.require_creative_commons and not details.is_creative_commons:
        return GateDecision(
            Verdict.SKIP,
            "video licence is %r, not %r" % (details.license, CREATIVE_COMMONS),
        )

    # 3. A livestream has no stable duration and no final file.
    if details.is_live_or_upcoming:
        return GateDecision(
            Verdict.DEFER,
            "video is %s — retry once the broadcast has finished"
            % details.live_broadcast_content,
        )

    # 4. Duration. An unknown duration is a DEFER: it is what the API returns
    #    for an in-progress stream, and guessing in either direction is worse
    #    than waiting.
    if details.duration_seconds is None:
        return GateDecision(
            Verdict.DEFER, "video duration is unknown — cannot apply the length limits"
        )

    max_seconds = config.max_source_duration_minutes * 60
    if details.duration_seconds > max_seconds:
        return GateDecision(
            Verdict.SKIP,
            "video is %dm long, over the %dm limit"
            % (details.duration_seconds // 60, config.max_source_duration_minutes),
        )
    if details.duration_seconds < config.min_source_duration_seconds:
        return GateDecision(
            Verdict.SKIP,
            "video is %ds long, under the %ds minimum — too short to clip from"
            % (details.duration_seconds, config.min_source_duration_seconds),
        )

    return GateDecision(Verdict.ALLOW)


def check_download_size(size_bytes: Optional[int], config: GateConfig) -> GateDecision:
    """Second gate, applied to the probed file size before fetching bytes.

    Separate from ``evaluate`` because the size is only known after the
    downloader has probed the formats, which costs a request. An unknown size
    is allowed through: yt-dlp often cannot estimate it, and refusing every
    unestimatable video would reject most of them. The download itself is
    capped independently, so an unknown size cannot run away.
    """
    if size_bytes is None:
        return GateDecision(Verdict.ALLOW)
    max_bytes = config.max_download_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        return GateDecision(
            Verdict.SKIP,
            "estimated download is %dMB, over the %dMB limit"
            % (size_bytes // (1024 * 1024), config.max_download_size_mb),
        )
    return GateDecision(Verdict.ALLOW)
