import os

import pytest

from src.utils.config import ConfigError, load_config, load_sources


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


GOOD_SOURCE = """
sources:
  - name: my_own_channel
    channel_id: "UCtesta0000000000000000A"
    enabled: true
    rights_cleared: true
    rights_note: "My own uploads."
"""


def test_loads_a_valid_source(tmp_path):
    sources = load_sources(write(tmp_path, "sources.yaml", GOOD_SOURCE))
    assert len(sources) == 1
    assert sources[0].name == "my_own_channel"
    assert sources[0].processable is True


def test_rights_gate_blocks_processing_but_not_discovery(tmp_path):
    text = GOOD_SOURCE.replace("rights_cleared: true", "rights_cleared: false")
    text = text.replace('rights_note: "My own uploads."', 'rights_note: ""')
    source = load_sources(write(tmp_path, "sources.yaml", text))[0]
    assert source.enabled is True
    assert source.processable is False


def test_disabled_source_is_not_processable(tmp_path):
    text = GOOD_SOURCE.replace("enabled: true", "enabled: false")
    assert load_sources(write(tmp_path, "sources.yaml", text))[0].processable is False


def test_rights_cleared_requires_a_note(tmp_path):
    text = GOOD_SOURCE.replace('rights_note: "My own uploads."', 'rights_note: ""')
    with pytest.raises(ConfigError, match="rights_note is empty"):
        load_sources(write(tmp_path, "sources.yaml", text))


@pytest.mark.parametrize(
    "bad_id",
    ["@somehandle", "https://www.youtube.com/@x", "UCtooshort", "PLnotachannel"],
)
def test_rejects_anything_that_is_not_a_uc_channel_id(tmp_path, bad_id):
    text = GOOD_SOURCE.replace("UCtesta0000000000000000A", bad_id)
    with pytest.raises(ConfigError, match="not a YouTube channel id"):
        load_sources(write(tmp_path, "sources.yaml", text))


def test_rejects_duplicate_channel_id(tmp_path):
    text = GOOD_SOURCE + """
  - name: a_different_name
    channel_id: "UCtesta0000000000000000A"
    enabled: true
    rights_cleared: false
"""
    with pytest.raises(ConfigError, match="already monitored"):
        load_sources(write(tmp_path, "sources.yaml", text))


def test_rejects_duplicate_name(tmp_path):
    text = GOOD_SOURCE + """
  - name: my_own_channel
    channel_id: "UCtestb0000000000000000B"
    enabled: true
    rights_cleared: false
"""
    with pytest.raises(ConfigError, match="duplicate source name"):
        load_sources(write(tmp_path, "sources.yaml", text))


def test_rejects_unknown_key_rather_than_ignoring_it(tmp_path):
    text = GOOD_SOURCE + "    rights_clered: true\n"
    with pytest.raises(ConfigError, match="unknown key"):
        load_sources(write(tmp_path, "sources.yaml", text))


def test_empty_sources_file_is_valid(tmp_path):
    assert load_sources(write(tmp_path, "sources.yaml", "sources: []\n")) == []


CONFIG = """
database:
  path: "data/test.db"
monitoring:
  backend: rss
  max_age_hours_on_discovery: 48
logging:
  level: "${TEST_LOG_LEVEL:-WARNING}"
"""


def test_env_substitution_uses_default_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_LOG_LEVEL", raising=False)
    config = load_config(write(tmp_path, "config.yaml", CONFIG))
    assert config.log_level == "WARNING"
    assert config.monitoring.max_age_hours_on_discovery == 48


def test_env_substitution_prefers_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_LOG_LEVEL", "debug")
    assert load_config(write(tmp_path, "config.yaml", CONFIG)).log_level == "DEBUG"


def test_missing_env_var_without_default_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR", raising=False)
    text = CONFIG.replace("${TEST_LOG_LEVEL:-WARNING}", "${NO_SUCH_VAR}")
    with pytest.raises(ConfigError, match="NO_SUCH_VAR"):
        load_config(write(tmp_path, "config.yaml", text))


def test_unknown_backend_is_refused_not_silently_accepted(tmp_path):
    text = CONFIG.replace("backend: rss", "backend: scraping")
    with pytest.raises(ConfigError, match="not implemented"):
        load_config(write(tmp_path, "config.yaml", text))


@pytest.mark.parametrize("backend", ["auto", "api", "rss"])
def test_every_documented_backend_loads(tmp_path, backend):
    text = CONFIG.replace("backend: rss", "backend: %s" % backend)
    assert load_config(write(tmp_path, "config.yaml", text)).monitoring.backend == backend


@pytest.mark.parametrize("bad", [0, 51, 100])
def test_api_max_results_is_bounded_by_the_api_page_size(tmp_path, bad):
    text = CONFIG.replace("  backend: rss", "  backend: api\n  api_max_results: %d" % bad)
    with pytest.raises(ConfigError, match="must be 1-50"):
        load_config(write(tmp_path, "config.yaml", text))


def test_feed_template_must_contain_the_placeholder(tmp_path):
    text = CONFIG.replace(
        "  backend: rss", '  backend: rss\n  feed_url_template: "https://x/feed"'
    )
    with pytest.raises(ConfigError, match="feed_url_template"):
        load_config(write(tmp_path, "config.yaml", text))


def test_the_repos_real_config_files_load():
    """The committed config must be valid, or a fresh clone cannot run."""
    config = load_config()
    sources = load_sources()
    assert config.monitoring.backend in ("auto", "api", "rss")
    # The shipped example must not be live: it is a placeholder, not a source.
    for source in sources:
        if source.name == "example_disabled_source":
            assert source.enabled is False
            assert source.rights_cleared is False
