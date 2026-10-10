"""Types shared by every upload detector.

Both detectors — the Data API and the Atom feed — normalise to ``FeedEntry``,
so ``source_monitor`` never branches on which one produced a result and a
third backend can be added without touching it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


@dataclass(frozen=True)
class FeedEntry:
    """One upload, as reported by whichever detector found it."""

    video_id: str
    title: str
    url: str
    published_at: Optional[str]
    channel_id: Optional[str] = None

    @property
    def published_dt(self) -> Optional[datetime]:
        if not self.published_at:
            return None
        try:
            return datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        dt = self.published_dt
        if dt is None:
            return None
        now = now or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 3600.0


class DetectorError(Exception):
    """A detector could not produce a result."""


class FeedUnchanged(Exception):
    """Nothing has changed since the last check (HTTP 304)."""
