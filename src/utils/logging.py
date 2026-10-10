"""Logging with the pipeline's standard context fields.

Section 23 of the spec requires every log line to be able to carry: timestamp,
job id, source video, clip id, platform, error and retry count. Rather than
threading those through every call signature, they live on a ``LogContext``
that is bound once per job and merged into each record.

Two formats: human-readable by default, newline-delimited JSON when
``logging.json`` is true (GitHub Actions log search is plain text, so the
human format is the better default there).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

CONTEXT_FIELDS = ("job_id", "source_video", "clip_id", "platform", "retry_count")


@dataclass
class LogContext:
    """Context merged into every record logged through a bound logger."""

    job_id: str
    source_video: Optional[str] = None
    clip_id: Optional[str] = None
    platform: Optional[str] = None
    retry_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def new_job_id(prefix: str) -> str:
    """A short, sortable-enough id for one pipeline run.

    In CI the GitHub run id is more useful than a random one, because it links
    the log line straight back to the workflow run that produced it.
    """
    run_id = os.environ.get("GITHUB_RUN_ID")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if run_id:
        return "%s-gh%s.%s" % (prefix, run_id, run_attempt)
    return "%s-%s" % (prefix, uuid.uuid4().hex[:8])


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = "%s %-7s %s" % (
            self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            record.levelname,
            record.getMessage(),
        )
        extras = []
        for key in CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is None or (key == "retry_count" and not value):
                continue
            extras.append("%s=%s" % (key, value))
        for key, value in sorted(getattr(record, "fields", {}).items()):
            extras.append("%s=%s" % (key, value))
        if extras:
            base = "%s  [%s]" % (base, " ".join(extras))
        if record.exc_info:
            base = "%s\n%s" % (base, self.formatException(record.exc_info))
        return base


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def configure(level: str = "INFO", json_output: bool = False) -> None:
    """Install the root handler. Idempotent, so tests and the CLI can both call it."""
    global _configured
    root = logging.getLogger()
    if _configured:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json_output else _HumanFormatter())
    root.handlers = [handler]
    root.setLevel(level)
    _configured = True


class ContextLogger(logging.LoggerAdapter):
    """A logger carrying a LogContext, with ``bind()`` for per-step detail."""

    def process(self, msg: Any, kwargs: Dict[str, Any]) -> Any:
        extra = dict(self.extra or {})
        extra.setdefault("fields", {})
        caller_fields = kwargs.pop("fields", None)
        if caller_fields:
            merged = dict(extra["fields"])
            merged.update(caller_fields)
            extra["fields"] = merged
        kwargs["extra"] = extra
        return msg, kwargs

    def bind(self, **updates: Any) -> "ContextLogger":
        """Return a new logger with extra context fields set or overridden."""
        extra = dict(self.extra or {})
        fields = dict(extra.get("fields", {}))
        for key, value in updates.items():
            if key in CONTEXT_FIELDS:
                extra[key] = value
            else:
                fields[key] = value
        extra["fields"] = fields
        return ContextLogger(self.logger, extra)


def get_logger(name: str, context: Optional[LogContext] = None) -> ContextLogger:
    """Get a context-carrying logger for a module."""
    extra: Dict[str, Any] = {"fields": {}}
    if context is not None:
        extra.update(context.as_dict())
    return ContextLogger(logging.getLogger(name), extra)
