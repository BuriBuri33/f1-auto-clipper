"""Loading .env.

This exists because `.env.example` instructed the reader to "copy to .env for
local runs" while no code ever read `.env` — so copying it did nothing. Same
defect class as the missing template key: the instructions and the mechanism
had drifted apart, and only the newcomer following the instructions would
have noticed.
"""

import os

from src.utils import config


def write_env(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def test_values_are_loaded_into_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("DOTENV_TEST_A", raising=False)
    path = write_env(tmp_path, "DOTENV_TEST_A=hello\n")
    assert config.load_dotenv(path) == 1
    assert os.environ["DOTENV_TEST_A"] == "hello"


def test_the_real_environment_wins_by_default(tmp_path, monkeypatch):
    """The important one: in CI the value is a GitHub Actions secret, and a
    stale committed .env must never be able to shadow it."""
    monkeypatch.setenv("DOTENV_TEST_B", "from-the-environment")
    path = write_env(tmp_path, "DOTENV_TEST_B=from-the-file\n")
    config.load_dotenv(path)
    assert os.environ["DOTENV_TEST_B"] == "from-the-environment"


def test_override_is_available_but_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("DOTENV_TEST_C", "from-the-environment")
    path = write_env(tmp_path, "DOTENV_TEST_C=from-the-file\n")
    config.load_dotenv(path, override=True)
    assert os.environ["DOTENV_TEST_C"] == "from-the-file"


def test_an_empty_value_in_the_file_does_not_mask_a_real_one(tmp_path, monkeypatch):
    """.env.example ships empty placeholders; copying it verbatim must not
    blank out a variable that is genuinely set."""
    monkeypatch.setenv("DOTENV_TEST_D", "real")
    path = write_env(tmp_path, "DOTENV_TEST_D=\n")
    config.load_dotenv(path)
    assert os.environ["DOTENV_TEST_D"] == "real"


def test_comments_and_blank_lines_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("DOTENV_TEST_E", raising=False)
    path = write_env(tmp_path, "# a comment\n\n   \nDOTENV_TEST_E=x\n# trailing\n")
    assert config.load_dotenv(path) == 1
    assert os.environ["DOTENV_TEST_E"] == "x"


def test_an_export_prefix_is_tolerated(tmp_path, monkeypatch):
    """People paste shell-style lines into .env all the time."""
    monkeypatch.delenv("DOTENV_TEST_F", raising=False)
    path = write_env(tmp_path, "export DOTENV_TEST_F=y\n")
    config.load_dotenv(path)
    assert os.environ["DOTENV_TEST_F"] == "y"


def test_surrounding_quotes_are_stripped_once(tmp_path, monkeypatch):
    for raw, expected in [('"q"', "q"), ("'q'", "q"), ('"a b"', "a b"),
                          ('""', ""), ('"un\'matched', '"un\'matched')]:
        monkeypatch.delenv("DOTENV_TEST_G", raising=False)
        path = write_env(tmp_path, "DOTENV_TEST_G=%s\n" % raw)
        config.load_dotenv(path, override=True)
        assert os.environ.get("DOTENV_TEST_G", "") == expected, raw


def test_a_value_containing_equals_is_kept_whole(tmp_path, monkeypatch):
    """Base64 and JWT-ish credentials routinely contain '='."""
    monkeypatch.delenv("DOTENV_TEST_H", raising=False)
    path = write_env(tmp_path, "DOTENV_TEST_H=abc==def=\n")
    config.load_dotenv(path, override=True)
    assert os.environ["DOTENV_TEST_H"] == "abc==def="


def test_a_missing_file_is_normal_not_an_error(tmp_path):
    """CI has no .env at all."""
    assert config.load_dotenv(tmp_path / "nope.env") == 0


def test_malformed_lines_are_skipped_without_raising(tmp_path, monkeypatch):
    monkeypatch.delenv("DOTENV_TEST_I", raising=False)
    path = write_env(tmp_path, "no equals here\n=novalue\nDOTENV_TEST_I=ok\n")
    config.load_dotenv(path)
    assert os.environ["DOTENV_TEST_I"] == "ok"


def test_the_cli_loads_dotenv_before_reading_the_environment(tmp_path, monkeypatch):
    """Regression guard: main() must call it, or the whole feature is inert."""
    import src.main as main_module
    import inspect
    source = inspect.getsource(main_module.main)
    assert "load_dotenv()" in source
