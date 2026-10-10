"""YouTube publishing: OAuth refresh, resumable upload, and its resume path.

Protocol shapes (Location header on session-open, 308+Range on an
interrupted/queried upload) are Google's documented resumable-upload
contract, not independently verified against a live account — this project
has no YouTube OAuth credentials configured yet. See docs/LIMITS.md.
"""

from pathlib import Path

import pytest
import requests

from src.publishing import youtube
from src.publishing.types import PublishAuthError, PublishError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text or (str(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, data=None, json=None, params=None, headers=None, timeout=None):
        return self._next("post", url, data=data, json=json, params=params,
                          headers=headers, timeout=timeout)

    def put(self, url, data=None, headers=None, timeout=None):
        return self._next("put", url, data=data, headers=headers, timeout=timeout)


@pytest.fixture()
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\0" * 5000)
    return path


# --- token refresh -----------------------------------------------------


def test_refresh_access_token_returns_the_token_on_success():
    session = FakeSession([FakeResponse(200, {"access_token": "TOK123"})])
    token = youtube.refresh_access_token("id", "secret", "refresh", session=session)
    assert token == "TOK123"
    assert session.calls[0]["data"]["grant_type"] == "refresh_token"


def test_refresh_access_token_raises_auth_error_on_400():
    session = FakeSession([
        FakeResponse(400, {"error": "invalid_grant", "error_description": "Token expired"})
    ])
    with pytest.raises(PublishAuthError, match="invalid_grant"):
        youtube.refresh_access_token("id", "secret", "bad", session=session)


def test_refresh_access_token_raises_on_a_connection_error():
    session = FakeSession([requests.ConnectionError("dns failure")])
    with pytest.raises(PublishError, match="dns failure"):
        youtube.refresh_access_token("id", "secret", "refresh", session=session)


def test_refresh_access_token_raises_on_an_unexpected_status():
    session = FakeSession([FakeResponse(503, text="down")])
    with pytest.raises(PublishError):
        youtube.refresh_access_token("id", "secret", "refresh", session=session)


# --- resumable upload, happy path ---------------------------------------


def test_upload_opens_a_session_then_puts_the_whole_file(clip):
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/session1"}),
        FakeResponse(200, {"id": "VID123"}),
    ])
    result = youtube.upload(
        clip, "A Title", "A description", ["F1", "Motorsport"],
        "id", "secret", "refresh", privacy_status="private",
        session=session, access_token="TOK", sleep=lambda _: None,
    )
    assert result.video_id == "VID123"
    assert result.url == "https://www.youtube.com/watch?v=VID123"
    assert result.privacy_status == "private"

    open_call = session.calls[0]
    assert open_call["json"]["status"]["privacyStatus"] == "private"
    assert open_call["json"]["snippet"]["title"] == "A Title"
    assert open_call["headers"]["X-Upload-Content-Length"] == "5000"

    put_call = session.calls[1]
    assert put_call["url"] == "https://upload.example/session1"
    assert put_call["headers"]["Content-Range"] == "bytes 0-4999/5000"


def test_upload_fetches_its_own_token_when_none_is_supplied(clip):
    session = FakeSession([
        FakeResponse(200, {"access_token": "FRESH"}),
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        FakeResponse(200, {"id": "VID1"}),
    ])
    youtube.upload(
        clip, "T", "D", [], "id", "secret", "refresh",
        session=session, sleep=lambda _: None,
    )
    assert session.calls[1]["headers"]["Authorization"] == "Bearer FRESH"


def test_upload_reports_quota_spend_on_success(clip):
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        FakeResponse(200, {"id": "VID1"}),
    ])
    spent = []
    youtube.upload(
        clip, "T", "D", [], "id", "secret", "refresh", session=session,
        access_token="TOK", sleep=lambda _: None, on_quota_spend=spent.append,
    )
    assert spent == [1600]


def test_upload_raises_if_the_file_does_not_exist(tmp_path):
    with pytest.raises(PublishError, match="does not exist"):
        youtube.upload(
            tmp_path / "nope.mp4", "T", "D", [], "id", "secret", "refresh",
            session=FakeSession([]), access_token="TOK",
        )


# --- resumable upload, the resume path ----------------------------------


def test_upload_resumes_after_a_dropped_connection(clip):
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        requests.ConnectionError("dropped"),
        # Resume-offset query: 2000 bytes already received.
        FakeResponse(308, headers={"Range": "bytes=0-1999"}),
        FakeResponse(200, {"id": "VID1"}),
    ])
    result = youtube.upload(
        clip, "T", "D", [], "id", "secret", "refresh", session=session,
        access_token="TOK", sleep=lambda _: None, max_retries=3,
    )
    assert result.video_id == "VID1"

    # The resumed PUT starts at byte 2000, not 0.
    resumed_put = session.calls[-1]
    assert resumed_put["headers"]["Content-Range"] == "bytes 2000-4999/5000"


def test_upload_resumes_from_zero_when_nothing_was_received_yet(clip):
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        FakeResponse(500, text="server error"),
        FakeResponse(308, headers={}),  # no Range header: nothing received
        FakeResponse(200, {"id": "VID1"}),
    ])
    youtube.upload(
        clip, "T", "D", [], "id", "secret", "refresh", session=session,
        access_token="TOK", sleep=lambda _: None, max_retries=3,
    )
    resumed_put = session.calls[-1]
    assert resumed_put["headers"]["Content-Range"] == "bytes 0-4999/5000"


def test_upload_treats_308_on_the_offset_query_as_success_if_fully_received(clip):
    """A retry can find the server already has everything, if the earlier
    'failure' response was itself lost in transit rather than the request."""
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        requests.ConnectionError("dropped after success"),
        FakeResponse(200, {"id": "VID1"}),  # offset query: already complete
    ])
    result = youtube.upload(
        clip, "T", "D", [], "id", "secret", "refresh", session=session,
        access_token="TOK", sleep=lambda _: None, max_retries=3,
    )
    assert result.video_id == "VID1"


def test_upload_gives_up_after_max_retries(clip):
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        FakeResponse(500, text="1"),
        FakeResponse(308, headers={}),
        FakeResponse(500, text="2"),
        FakeResponse(308, headers={}),
    ])
    with pytest.raises(PublishError):
        youtube.upload(
            clip, "T", "D", [], "id", "secret", "refresh", session=session,
            access_token="TOK", sleep=lambda _: None, max_retries=2,
        )


def test_upload_raises_auth_error_if_the_token_is_rejected_mid_upload(clip):
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        FakeResponse(401, text="unauthorized"),
    ])
    with pytest.raises(PublishAuthError):
        youtube.upload(
            clip, "T", "D", [], "id", "secret", "refresh", session=session,
            access_token="TOK", sleep=lambda _: None,
        )


def test_upload_raises_immediately_on_a_non_retryable_rejection(clip):
    session = FakeSession([
        FakeResponse(200, headers={"Location": "https://upload.example/s"}),
        FakeResponse(400, text="malformed body"),
    ])
    with pytest.raises(PublishError, match="malformed body"):
        youtube.upload(
            clip, "T", "D", [], "id", "secret", "refresh", session=session,
            access_token="TOK", sleep=lambda _: None,
        )


def test_opening_a_session_without_a_location_header_is_an_error(clip):
    session = FakeSession([FakeResponse(200, headers={})])
    with pytest.raises(PublishError, match="no session URL"):
        youtube.upload(
            clip, "T", "D", [], "id", "secret", "refresh", session=session,
            access_token="TOK", sleep=lambda _: None,
        )


def test_opening_a_session_raises_auth_error_on_401():
    session = FakeSession([FakeResponse(401, text="bad token")])
    with pytest.raises(PublishAuthError):
        youtube._open_resumable_session(
            "TOK", "T", "D", [], "private", "17", 100, session, 10,
        )
