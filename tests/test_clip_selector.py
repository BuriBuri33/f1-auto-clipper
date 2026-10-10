"""The Gemini clip selector: request shape, strict validation, error handling.

Error shapes (bad key -> 400/API_KEY_INVALID, malformed request -> 400) are
taken from a real call against the live API on 2026-10-10; RESOURCE_EXHAUSTED
is Google's documented convention for quota, not verified by triggering a
real exhaustion (that would spend the project's own free allowance proving
it) — see the comment in clip_selector.py.
"""

import pytest
import requests

from src.analysis.candidates import CandidateWindow
from src.analysis.clip_selector import GeminiClipSelector
from src.analysis.types import AnalysisError, AnalysisQuotaExceeded
from src.transcription.types import Segment


def window(start, end, text="Something happens."):
    return CandidateWindow([Segment(start, end, text)])


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "json": json, "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok_body(clips):
    import json as jsonlib
    text = jsonlib.dumps({"clips": clips})
    return FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    })


def clip_entry(start, end, hook_score=70, emotional_score=70, context_score=70,
               payoff_score=70, shareability_score=70, reason="r", hook="h"):
    return {"start": start, "end": end, "hook_score": hook_score,
            "emotional_score": emotional_score, "context_score": context_score,
            "payoff_score": payoff_score, "shareability_score": shareability_score,
            "reason": reason, "hook": hook}


def make(session, **kw):
    return GeminiClipSelector(api_key="KEY", session=session, sleep=lambda _: None, **kw)


def api_error(code, status, message="error", reason=None):
    error = {"code": code, "status": status, "message": message}
    if reason:
        error["details"] = [{"reason": reason}]
    return FakeResponse(code, {"error": error}, text=message)


# --- request shape ----------------------------------------------------------


def test_the_request_uses_a_structured_response_schema_and_thinking_budget_zero():
    session = FakeSession([ok_body([])])
    make(session).score_windows([window(0, 20)])
    body = session.calls[0]["json"]
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert "responseSchema" in cfg
    assert cfg["thinkingConfig"]["thinkingBudget"] == 0


def test_thinking_budget_is_configurable():
    session = FakeSession([ok_body([])])
    make(session, thinking_budget=-1).score_windows([window(0, 20)])
    assert session.calls[0]["json"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == -1


def test_the_api_key_is_sent_as_a_query_param_not_a_header():
    """Matches the live API's auth convention."""
    session = FakeSession([ok_body([])])
    make(session).score_windows([window(0, 20)])
    assert session.calls[0]["params"]["key"] == "KEY"


def test_empty_windows_makes_no_request():
    session = FakeSession([])
    assert make(session).score_windows([]) == []
    assert session.calls == []


def test_a_missing_key_is_refused_at_construction():
    with pytest.raises(AnalysisError, match="no Gemini API key"):
        GeminiClipSelector(api_key="")


# --- strict validation: reject, never coerce --------------------------------


def test_a_valid_entry_round_trips():
    w = window(10.0, 40.0)
    session = FakeSession([ok_body([clip_entry(10.0, 40.0)])])
    result = make(session).score_windows([w])
    assert len(result) == 1
    assert result[0].start == 10.0 and result[0].end == 40.0
    assert result[0].method == "llm"


def test_an_out_of_range_sub_score_is_rejected_not_clamped():
    w = window(10.0, 40.0)
    session = FakeSession([ok_body([clip_entry(10.0, 40.0, hook_score=150)])])
    assert make(session).score_windows([w]) == []


def test_a_missing_field_is_rejected():
    w = window(10.0, 40.0)
    entry = clip_entry(10.0, 40.0)
    del entry["reason"]
    session = FakeSession([ok_body([entry])])
    assert make(session).score_windows([w]) == []


def test_an_empty_reason_or_hook_is_rejected():
    w = window(10.0, 40.0)
    session = FakeSession([ok_body([clip_entry(10.0, 40.0, reason="   ")])])
    assert make(session).score_windows([w]) == []


def test_a_timestamp_the_model_invented_is_rejected():
    """The core of 'strict JSON': the span must match one we actually sent."""
    w = window(10.0, 40.0)
    session = FakeSession([ok_body([clip_entry(999.0, 1030.0)])])
    assert make(session).score_windows([w]) == []


def test_a_timestamp_within_float_tolerance_is_accepted():
    w = window(10.0, 40.0)
    session = FakeSession([ok_body([clip_entry(10.3, 39.8)])])
    result = make(session).score_windows([w])
    assert len(result) == 1
    # Stored candidate uses the WINDOW's exact span, not the model's.
    assert result[0].start == 10.0 and result[0].end == 40.0


def test_an_inverted_span_is_rejected():
    w = window(10.0, 40.0)
    session = FakeSession([ok_body([clip_entry(40.0, 10.0)])])
    assert make(session).score_windows([w]) == []


def test_one_bad_entry_does_not_discard_the_rest():
    w1, w2 = window(0, 20), window(100, 130)
    session = FakeSession([ok_body([
        clip_entry(0, 20, hook_score=999),       # rejected
        clip_entry(100, 130),                     # kept
    ])])
    result = make(session).score_windows([w1, w2])
    assert len(result) == 1
    assert result[0].start == 100.0


def test_a_non_dict_entry_is_skipped():
    w = window(0, 20)
    session = FakeSession([ok_body(["not a dict", clip_entry(0, 20)])])
    result = make(session).score_windows([w])
    assert len(result) == 1


def test_a_response_with_no_clips_list_is_an_error():
    session = FakeSession([FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
    })])
    with pytest.raises(AnalysisError, match="no 'clips' list"):
        make(session).score_windows([window(0, 20)])


def test_malformed_json_text_is_an_error():
    session = FakeSession([FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": "not json {{{"}]}}],
    })])
    with pytest.raises(AnalysisError, match="not valid JSON"):
        make(session).score_windows([window(0, 20)])


def test_a_response_with_no_text_part_is_an_error():
    session = FakeSession([FakeResponse(200, {"candidates": []})])
    with pytest.raises(AnalysisError, match="no text part"):
        make(session).score_windows([window(0, 20)])


# --- error classification ---------------------------------------------------


def test_a_bad_key_is_reported_with_where_to_get_one():
    session = FakeSession([api_error(400, "INVALID_ARGUMENT", "API key not valid.",
                                     reason="API_KEY_INVALID")])
    with pytest.raises(AnalysisError, match="aistudio.google.com/apikey"):
        make(session).score_windows([window(0, 20)])
    assert len(session.calls) == 1


def test_resource_exhausted_raises_quota_exceeded_and_is_not_retried():
    session = FakeSession([api_error(429, "RESOURCE_EXHAUSTED", "quota exceeded")])
    with pytest.raises(AnalysisQuotaExceeded, match="queued"):
        make(session).score_windows([window(0, 20)])
    assert len(session.calls) == 1


def test_a_plain_429_without_resource_exhausted_is_retried():
    session = FakeSession([
        FakeResponse(429, {"error": {"status": "UNAVAILABLE"}}),
        ok_body([clip_entry(0, 20)]),
    ])
    result = make(session).score_windows([window(0, 20)])
    assert len(result) == 1
    assert len(session.calls) == 2


def test_server_errors_are_retried_then_succeed():
    session = FakeSession([FakeResponse(500, {}), ok_body([clip_entry(0, 20)])])
    result = make(session).score_windows([window(0, 20)])
    assert len(result) == 1


def test_a_malformed_request_400_is_not_retried():
    session = FakeSession([api_error(400, "INVALID_ARGUMENT",
                                     "Invalid JSON payload received.")])
    with pytest.raises(AnalysisError, match="Invalid JSON payload"):
        make(session).score_windows([window(0, 20)])
    assert len(session.calls) == 1


def test_connection_errors_are_retried():
    session = FakeSession([requests.ConnectionError("dns"), ok_body([clip_entry(0, 20)])])
    result = make(session).score_windows([window(0, 20)])
    assert len(result) == 1


def test_retries_eventually_give_up():
    session = FakeSession([FakeResponse(500, {})] * 4)
    with pytest.raises(AnalysisError, match="HTTP 500"):
        make(session, max_retries=4).score_windows([window(0, 20)])
