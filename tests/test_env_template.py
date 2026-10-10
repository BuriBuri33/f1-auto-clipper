"""Every environment variable the code reads must appear in .env.example.

This test exists because of a real defect found in the 2026-10-10 spec audit:
`YOUTUBE_API_KEY` became *required* in Phase 2, was documented in the README,
and was never added to `.env.example`. Nothing failed. A fresh clone that
followed the setup instructions would have had every video silently deferred
with no indication why.

The failure mode is specific and recurs: a new required input has a
*declaration* surface (the template someone copies) and an *explanation*
surface (the prose someone reads). Updating only the prose is invisible to
the author, who already knows the variable exists, and fatal to the newcomer.
So this is a test rather than a convention.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SRC = REPO_ROOT / "src"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Variables the runtime provides, not the operator.
PROVIDED_BY_THE_HARNESS = {"GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_STEP_SUMMARY"}


def declared_in_template():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))


def read_by_the_code():
    """Variables the source actually reads from the environment."""
    found = set()
    patterns = (
        re.compile(r"os\.environ\.get\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"),
        re.compile(r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\]"),
        re.compile(r"os\.getenv\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']"),
    )
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            found.update(pattern.findall(text))
    return found


def substituted_in_config():
    """Variables referenced as ${VAR} / ${VAR:-default} in the YAML config.

    Comments are stripped first: the config explains its own substitution
    syntax using a literal `${VAR}`, and scanning comments picks that up as a
    variable named VAR.
    """
    found = set()
    for path in (REPO_ROOT / "config").glob("*.yaml"):
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
            lines.append(stripped)
        found.update(
            re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?::-[^}]*)?\}", "\n".join(lines))
        )
    return found


def used_by_workflows():
    """Secrets the workflows pass through as environment variables."""
    found = set()
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        found.update(re.findall(r"secrets\.([A-Z][A-Z0-9_]*)", text))
    return found


def test_the_template_exists_and_declares_something():
    assert ENV_EXAMPLE.exists()
    assert declared_in_template(), ".env.example declares no variables"


def test_every_variable_the_code_reads_is_in_the_template():
    missing = read_by_the_code() - declared_in_template() - PROVIDED_BY_THE_HARNESS
    assert not missing, (
        "read by src/ but absent from .env.example: %s\n"
        "A variable the code reads and the template omits is one a fresh "
        "clone cannot know to set." % ", ".join(sorted(missing))
    )


def test_every_variable_the_config_substitutes_is_in_the_template():
    missing = substituted_in_config() - declared_in_template()
    assert not missing, (
        "substituted in config/*.yaml but absent from .env.example: %s"
        % ", ".join(sorted(missing))
    )


def test_every_workflow_secret_is_in_the_template():
    missing = used_by_workflows() - declared_in_template()
    assert not missing, (
        "passed by a workflow but absent from .env.example: %s\n"
        "The template is also the list of GitHub Secrets to configure."
        % ", ".join(sorted(missing))
    )


def test_the_spec_mandated_variables_are_all_present():
    """Spec section 24 names these explicitly."""
    required = {
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
        "META_ACCESS_TOKEN", "INSTAGRAM_ACCOUNT_ID",
        "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN",
        "LLM_API_KEY", "TRANSCRIPTION_API_KEY",
    }
    missing = required - declared_in_template()
    assert not missing, "spec section 24 requires: %s" % ", ".join(sorted(missing))


# A non-secret default in the template is useful (LOG_LEVEL=INFO documents the
# expected value). A credential with a value in it is a leak. Only the latter
# is a defect, so the rule is keyed on the name, not on all assignments.
_CREDENTIAL_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "ID")


def test_no_credential_in_the_template_has_a_value():
    """A credential with a value committed to .env.example is a leak."""
    offenders = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        looks_like_a_credential = any(m in name for m in _CREDENTIAL_MARKERS)
        if looks_like_a_credential and value.strip():
            offenders.append(name)
    assert not offenders, (
        "these credentials have values committed in .env.example: %s"
        % ", ".join(offenders)
    )
