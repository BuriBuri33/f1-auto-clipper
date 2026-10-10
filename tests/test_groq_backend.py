import pytest
import requests

from src.transcription.groq_backend import (
    FREE_TIER_MAX_BYTES,
    GroqTranscriber,
    parse_verbose_json,
)
from src.transcription.types import TranscriptionError, TranscriptionQuotaExceeded


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, files=None, data=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "data": dict(data or {}), "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def audio(tmp_path, size=1024, name="a.mp3"):
    p = tmp_path / name
    p.write_bytes(b"\0" * size)
    return p


def body(segments=None, text="hello", duration=10.0, language="en"):
    payload = {"text": text, "duration": duration, "language": language}
    if segments is not None:
        payload["segments"] = segments
    return payload


def make(session, **kw):
    return GroqTranscriber(api_key="KEY", session=session, sleep=lambda _: None, **kw)


# --- request shape ---------------------------------------------------------


def test_the_request_asks_for_verbose_json_so_timestamps_come_back(tmp_path):
    session = FakeSession([FakeResponse(200, body())])
    make(session).transcribe_file(audio(tmp_path))
    assert session.calls[0]["data"]["response_format"] == "verbose_json"


def test_the_model_and_bearer_token_are_sent(tmp_path):
    session = FakeSession([FakeResponse(200, body())])
    make(session, model="whisper-large-v3").transcribe_file(audio(tmp_path))
    assert session.calls[0]["data"]["model"] == "whisper-large-v3"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer KEY"


def test_language_is_sent_only_when_configured(tmp_path):
    session = FakeSession([FakeResponse(200, body()), FakeResponse(200, body())])
    make(session).transcribe_file(audio(tmp_path))
    assert "language" not in session.calls[0]["data"]
    make(session, language="en").transcribe_file(audio(tmp_path))
    assert session.calls[1]["data"]["language"] == "en"


def test_a_missing_key_is_refused_at_construction():
    with pytest.raises(TranscriptionError, match="no Groq API key"):
        GroqTranscriber(api_key="")


# --- the size cap, which is the binding free-tier constraint --------------


def test_an_oversized_file_is_refused_locally_with_actionable_advice(tmp_path):
    """The API's own error for an oversized body is far less useful."""
    session = FakeSession([])
    big = audio(tmp_path, size=FREE_TIER_MAX_BYTES + 1)
    with pytest.raises(TranscriptionError, match="split_audio"):
        make(session).transcribe_file(big)
    assert session.calls == [], "must not waste an upload"


def test_a_file_exactly_at_the_cap_is_allowed(tmp_path):
    session = FakeSession([FakeResponse(200, body())])
    make(session).transcribe_file(audio(tmp_path, size=FREE_TIER_MAX_BYTES))
    assert len(session.calls) == 1


# --- error handling --------------------------------------------------------


def test_a_bad_key_is_reported_with_where_to_get_one(tmp_path):
    session = FakeSession([FakeResponse(401, {"error": "bad key"})])
    with pytest.raises(TranscriptionError, match="console.groq.com/keys"):
        make(session).transcribe_file(audio(tmp_path))


def test_a_daily_allowance_is_quota_exceeded_and_is_not_retried(tmp_path):
    """Distinct from a per-minute limit: this one waits hours, not seconds."""
    session = FakeSession([
        FakeResponse(429, {"error": {"message": "rate limit reached for audio "
                                                "seconds per day"}})
    ])
    with pytest.raises(TranscriptionQuotaExceeded, match="queued"):
        make(session).transcribe_file(audio(tmp_path))
    assert len(session.calls) == 1


def test_a_long_retry_after_is_also_read_as_a_daily_limit(tmp_path):
    session = FakeSession([
        FakeResponse(429, {"error": {"message": "slow down"}},
                     headers={"retry-after": "7200"})
    ])
    with pytest.raises(TranscriptionQuotaExceeded):
        make(session).transcribe_file(audio(tmp_path))


def test_a_per_minute_rate_limit_is_retried_then_succeeds(tmp_path):
    session = FakeSession([
        FakeResponse(429, {"error": {"message": "rate limit reached, please "
                                                "try again in 2s"}},
                     headers={"retry-after": "2"}),
        FakeResponse(200, body()),
    ])
    payload = make(session).transcribe_file(audio(tmp_path))
    assert payload["text"] == "hello"
    assert len(session.calls) == 2


def test_a_413_points_at_the_bitrate_setting(tmp_path):
    session = FakeSession([FakeResponse(413, {"error": "too large"})])
    with pytest.raises(TranscriptionError, match="bitrate_kbps"):
        make(session).transcribe_file(audio(tmp_path))


def test_server_errors_are_retried(tmp_path):
    session = FakeSession([FakeResponse(500, {}), FakeResponse(200, body())])
    assert make(session).transcribe_file(audio(tmp_path))["text"] == "hello"


def test_connection_errors_are_retried(tmp_path):
    session = FakeSession([requests.ConnectionError("dns"), FakeResponse(200, body())])
    assert make(session).transcribe_file(audio(tmp_path))["text"] == "hello"


def test_retries_eventually_give_up(tmp_path):
    session = FakeSession([FakeResponse(500, {})] * 3)
    with pytest.raises(TranscriptionError, match="HTTP 500"):
        make(session, max_retries=3).transcribe_file(audio(tmp_path))


def test_a_non_json_success_body_is_an_error(tmp_path):
    session = FakeSession([FakeResponse(200, None)])
    with pytest.raises(TranscriptionError, match="non-JSON"):
        make(session).transcribe_file(audio(tmp_path))


# --- parsing ---------------------------------------------------------------


def test_segments_are_parsed_with_their_timestamps():
    segments = parse_verbose_json(body(segments=[
        {"start": 0.0, "end": 2.5, "text": " Lights out "},
        {"start": 2.5, "end": 5.0, "text": " and away we go "},
    ]))
    assert [s.text for s in segments] == ["Lights out", "and away we go"]
    assert segments[1].start == 2.5


def test_a_segment_with_unusable_timestamps_is_skipped_not_fatal():
    segments = parse_verbose_json(body(segments=[
        {"start": "x", "end": 2.0, "text": "bad"},
        {"text": "no times"},
        {"start": 3.0, "end": 4.0, "text": "good"},
    ]))
    assert [s.text for s in segments] == ["good"]


def test_an_inverted_segment_is_skipped():
    segments = parse_verbose_json(
        body(segments=[{"start": 5.0, "end": 1.0, "text": "x"}], text="", duration=0.0)
    )
    assert segments == []


def test_unusable_segments_fall_back_to_the_whole_text_rather_than_nothing():
    """A deliberate trade: the text is still worth having, and returning
    nothing would be indistinguishable from silence. Timing fidelity is lost,
    which `chunk_count`/`segments` in the stored transcript makes visible."""
    segments = parse_verbose_json(
        body(segments=[{"start": 5.0, "end": 1.0, "text": "x"}],
             text="salvaged text", duration=10.0)
    )
    assert len(segments) == 1
    assert segments[0].text == "salvaged text"
    assert (segments[0].start, segments[0].end) == (0.0, 10.0)


def test_a_response_with_no_segments_falls_back_to_one_whole_span():
    """Happens for very short audio; returning nothing would look like silence."""
    segments = parse_verbose_json(body(segments=None, text="just a few words",
                                       duration=3.0))
    assert len(segments) == 1
    assert (segments[0].start, segments[0].end) == (0.0, 3.0)
    assert segments[0].text == "just a few words"


def test_an_empty_segment_list_falls_back_to_the_whole_text():
    segments = parse_verbose_json(body(segments=[], text="words", duration=3.0))
    assert len(segments) == 1


def test_genuinely_empty_audio_yields_no_segments():
    assert parse_verbose_json(body(segments=[], text="", duration=3.0)) == []
    assert parse_verbose_json(body(segments=None, text="x", duration=0.0)) == []
