"""Configuration loading.

Two files, loaded independently so a bad source list cannot stop the rest of
the pipeline from reporting status:

    config/config.yaml   pipeline settings
    config/sources.yaml  the channels to monitor

Both support ``${VAR}`` and ``${VAR:-default}`` substitution from the
environment, which is how GitHub Actions secrets reach the config without
anything secret being committed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
DEFAULT_SOURCES_PATH = REPO_ROOT / "config" / "sources.yaml"
DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"

# ${NAME} or ${NAME:-default}
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised for any malformed or missing configuration."""


def load_dotenv(path: Optional[Path] = None, override: bool = False) -> int:
    """Load KEY=VALUE pairs from a .env file into the environment.

    Hand-rolled rather than taking a dependency on python-dotenv: this is
    thirty lines and the project rule is to prefer the standard library.

    **The real environment wins by default** (``override=False``). That
    precedence is the important part: in CI the values arrive as GitHub
    Actions secrets, and a stale committed .env must never be able to shadow
    them. Locally there is no competing value, so the file is used.

    Returns the number of variables actually set. A missing file is normal,
    not an error — CI has no .env at all.
    """
    path = Path(path) if path else DEFAULT_DOTENV_PATH
    if not path.exists():
        return 0

    applied = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        # Strip one matching pair of surrounding quotes, so a value with
        # trailing spaces can be expressed deliberately.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not override and os.environ.get(name):
            continue
        os.environ[name] = value
        applied += 1
    return applied


def _expand(value: Any) -> Any:
    """Recursively substitute ${VAR} / ${VAR:-default} in strings."""
    if isinstance(value, str):

        def repl(m: "re.Match[str]") -> str:
            name, default = m.group(1), m.group(2)
            env = os.environ.get(name)
            if env is not None and env != "":
                return env
            if default is not None:
                return default
            raise ConfigError(
                "config references ${%s} but it is not set in the environment "
                "and has no ${%s:-default}" % (name, name)
            )

        return _VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError("config file not found: %s" % path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("%s must contain a YAML mapping at the top level" % path)
    return _expand(raw)


@dataclass(frozen=True)
class Source:
    """One monitored channel."""

    name: str
    channel_id: str
    enabled: bool = True
    rights_cleared: bool = False
    rights_note: str = ""

    @property
    def processable(self) -> bool:
        """Whether videos from this source may be downloaded and published.

        Discovery is always allowed — recording that a video exists carries no
        rights implication. Everything downstream requires the rights gate.
        """
        return self.enabled and self.rights_cleared


VALID_BACKENDS = ("auto", "api", "rss")


@dataclass(frozen=True)
class MonitoringConfig:
    backend: str = "auto"
    feed_url_template: str = (
        "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    )
    api_max_results: int = 25
    max_age_hours_on_discovery: int = 72
    request_timeout_seconds: int = 20
    max_retries: int = 4
    backoff_base_seconds: float = 2.0


@dataclass(frozen=True)
class Config:
    database_path: Path
    monitoring: MonitoringConfig
    log_level: str = "INFO"
    log_json: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def section(self, name: str) -> Dict[str, Any]:
        """Return a config section not yet promoted to a typed dataclass.

        Later phases (clip, video, publishing, branding) read through this
        until they have their own types, so a key can be added to the YAML
        without a code change.
        """
        value = self.raw.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError("config section %r must be a mapping" % name)
        return value


_KNOWN_SOURCE_KEYS = {"name", "channel_id", "enabled", "rights_cleared", "rights_note"}
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def load_sources(path: Optional[Path] = None) -> List[Source]:
    """Load and validate config/sources.yaml."""
    path = path or DEFAULT_SOURCES_PATH
    raw = _load_yaml(path)
    entries = raw.get("sources") or []
    if not isinstance(entries, list):
        raise ConfigError("%s: 'sources' must be a list" % path)

    sources: List[Source] = []
    seen_names: Dict[str, int] = {}
    seen_channels: Dict[str, str] = {}

    for index, entry in enumerate(entries):
        where = "%s: sources[%d]" % (path, index)
        if not isinstance(entry, dict):
            raise ConfigError("%s must be a mapping" % where)

        unknown = set(entry) - _KNOWN_SOURCE_KEYS
        if unknown:
            raise ConfigError(
                "%s has unknown key(s): %s" % (where, ", ".join(sorted(unknown)))
            )

        name = entry.get("name")
        channel_id = entry.get("channel_id")
        if not name or not isinstance(name, str):
            raise ConfigError("%s needs a non-empty string 'name'" % where)
        if not channel_id or not isinstance(channel_id, str):
            raise ConfigError("%s needs a non-empty string 'channel_id'" % where)
        if not _CHANNEL_ID_RE.match(channel_id):
            raise ConfigError(
                "%s: channel_id %r is not a YouTube channel id. Expected the "
                "24-character UC... form, not an @handle or a channel URL."
                % (where, channel_id)
            )

        if name in seen_names:
            raise ConfigError(
                "%s: duplicate source name %r (also at sources[%d])"
                % (where, name, seen_names[name])
            )
        if channel_id in seen_channels:
            raise ConfigError(
                "%s: channel_id %s is already monitored as %r"
                % (where, channel_id, seen_channels[channel_id])
            )
        seen_names[name] = index
        seen_channels[channel_id] = name

        source = Source(
            name=name,
            channel_id=channel_id,
            enabled=bool(entry.get("enabled", True)),
            rights_cleared=bool(entry.get("rights_cleared", False)),
            rights_note=str(entry.get("rights_note", "")),
        )
        if source.rights_cleared and not source.rights_note.strip():
            raise ConfigError(
                "%s: rights_cleared is true but rights_note is empty. Record "
                "why this source is cleared — it is the audit trail." % where
            )
        sources.append(source)

    return sources


def load_config(path: Optional[Path] = None) -> Config:
    """Load and validate config/config.yaml."""
    path = path or DEFAULT_CONFIG_PATH
    raw = _load_yaml(path)

    db_raw = raw.get("database") or {}
    if not isinstance(db_raw, dict):
        raise ConfigError("%s: 'database' must be a mapping" % path)
    db_path = Path(db_raw.get("path", "data/state.db"))
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path

    mon_raw = raw.get("monitoring") or {}
    if not isinstance(mon_raw, dict):
        raise ConfigError("%s: 'monitoring' must be a mapping" % path)
    backend = str(mon_raw.get("backend", "auto"))
    if backend not in VALID_BACKENDS:
        raise ConfigError(
            "%s: monitoring.backend %r is not implemented. Valid values: %s"
            % (path, backend, ", ".join(VALID_BACKENDS))
        )
    api_max_results = int(mon_raw.get("api_max_results", 25))
    if not 1 <= api_max_results <= 50:
        raise ConfigError(
            "%s: monitoring.api_max_results must be 1-50 (the API caps a page "
            "at 50), got %d" % (path, api_max_results)
        )
    monitoring = MonitoringConfig(
        backend=backend,
        feed_url_template=str(
            mon_raw.get("feed_url_template", MonitoringConfig.feed_url_template)
        ),
        api_max_results=api_max_results,
        max_age_hours_on_discovery=int(mon_raw.get("max_age_hours_on_discovery", 72)),
        request_timeout_seconds=int(mon_raw.get("request_timeout_seconds", 20)),
        max_retries=int(mon_raw.get("max_retries", 4)),
        backoff_base_seconds=float(mon_raw.get("backoff_base_seconds", 2.0)),
    )
    if "{channel_id}" not in monitoring.feed_url_template:
        raise ConfigError(
            "%s: monitoring.feed_url_template must contain '{channel_id}'" % path
        )

    log_raw = raw.get("logging") or {}
    if not isinstance(log_raw, dict):
        raise ConfigError("%s: 'logging' must be a mapping" % path)

    return Config(
        database_path=db_path,
        monitoring=monitoring,
        log_level=str(log_raw.get("level", "INFO")).upper(),
        log_json=bool(log_raw.get("json", False)),
        raw=raw,
    )
