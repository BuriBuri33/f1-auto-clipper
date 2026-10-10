"""The pre-download gate.

This is the code that decides whether footage gets fetched at all, so it is
tested exhaustively and by behaviour, not by branch.
"""

import pytest

from src.download.gate import GateConfig, Verdict, check_download_size, evaluate
from src.monitoring.youtube_api import VideoDetails

CFG = GateConfig(
    require_creative_commons=True,
    max_source_duration_minutes=180,
    min_source_duration_seconds=120,
    max_download_size_mb=2000,
)


def details(license="creativeCommon", duration=600, live="none"):
    return VideoDetails(
        video_id="VID1",
        title="t",
        license=license,
        duration_seconds=duration,
        live_broadcast_content=live,
    )


def test_a_clean_creative_commons_video_is_allowed():
    assert evaluate(True, details(), CFG).verdict is Verdict.ALLOW


# --- rights ----------------------------------------------------------------


def test_an_uncleared_source_is_skipped():
    decision = evaluate(False, details(), CFG)
    assert decision.verdict is Verdict.SKIP
    assert "not rights-cleared" in decision.reason


def test_rights_are_checked_before_anything_else():
    """The operator must see the rights problem, not an incidental one."""
    bad_everything = details(license="youtube", duration=99999, live="live")
    decision = evaluate(False, bad_everything, CFG)
    assert "not rights-cleared" in decision.reason


def test_an_uncleared_source_is_skipped_even_with_no_details():
    """A missing details lookup must not become a way around the rights gate."""
    assert evaluate(False, None, CFG).verdict is Verdict.SKIP


# --- licence ---------------------------------------------------------------


def test_a_standard_licence_video_is_skipped_even_from_a_cleared_source():
    """The whole point: licence is per video, not per channel."""
    decision = evaluate(True, details(license="youtube"), CFG)
    assert decision.verdict is Verdict.SKIP
    assert "licence" in decision.reason


def test_an_unknown_licence_is_treated_as_restricted():
    assert evaluate(True, details(license=""), CFG).verdict is Verdict.SKIP
    assert evaluate(True, details(license="somethingNew"), CFG).verdict is Verdict.SKIP


def test_the_licence_check_can_be_disabled_for_owned_footage():
    cfg = GateConfig(require_creative_commons=False)
    assert evaluate(True, details(license="youtube"), cfg).verdict is Verdict.ALLOW


def test_disabling_the_licence_check_does_not_disable_the_rights_gate():
    cfg = GateConfig(require_creative_commons=False)
    assert evaluate(False, details(license="youtube"), cfg).verdict is Verdict.SKIP


# --- unverifiable ----------------------------------------------------------


def test_unverifiable_video_is_deferred_not_allowed_and_not_skipped():
    """Inability to verify is not evidence of eligibility, nor of ineligibility."""
    decision = evaluate(True, None, CFG)
    assert decision.verdict is Verdict.DEFER
    assert "could not verify" in decision.reason


# --- live ------------------------------------------------------------------


@pytest.mark.parametrize("live", ["live", "upcoming"])
def test_live_and_upcoming_are_deferred(live):
    decision = evaluate(True, details(live=live), CFG)
    assert decision.verdict is Verdict.DEFER
    assert "retry once the broadcast" in decision.reason


def test_a_finished_broadcast_is_fine():
    assert evaluate(True, details(live="none"), CFG).verdict is Verdict.ALLOW


# --- duration --------------------------------------------------------------


def test_too_long_is_skipped():
    decision = evaluate(True, details(duration=181 * 60), CFG)
    assert decision.verdict is Verdict.SKIP
    assert "over the 180m limit" in decision.reason


def test_too_short_is_skipped():
    decision = evaluate(True, details(duration=60), CFG)
    assert decision.verdict is Verdict.SKIP
    assert "under the 120s minimum" in decision.reason


def test_exactly_at_the_boundaries_is_allowed():
    assert evaluate(True, details(duration=180 * 60), CFG).verdict is Verdict.ALLOW
    assert evaluate(True, details(duration=120), CFG).verdict is Verdict.ALLOW


def test_unknown_duration_is_deferred_not_treated_as_zero():
    """A 0 default would sail past the maximum check and fail the minimum one
    for the wrong reason."""
    decision = evaluate(True, details(duration=None), CFG)
    assert decision.verdict is Verdict.DEFER
    assert "duration is unknown" in decision.reason


# --- size ------------------------------------------------------------------


def test_oversized_download_is_skipped():
    decision = check_download_size(2001 * 1024 * 1024, CFG)
    assert decision.verdict is Verdict.SKIP
    assert "over the 2000MB limit" in decision.reason


def test_a_normal_size_is_allowed():
    assert check_download_size(500 * 1024 * 1024, CFG).verdict is Verdict.ALLOW


def test_an_unknown_size_is_allowed_because_the_download_is_capped_anyway():
    assert check_download_size(None, CFG).verdict is Verdict.ALLOW


def test_exactly_at_the_size_limit_is_allowed():
    assert check_download_size(2000 * 1024 * 1024, CFG).verdict is Verdict.ALLOW
