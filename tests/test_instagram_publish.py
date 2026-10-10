"""Instagram Reels publishing: container create -> poll -> publish, plus the
hosting round-trip it depends on.

Graph API shapes follow Meta's documented container lifecycle
(status_code: IN_PROGRESS/FINISHED/ERROR/EXPIRED); not verified against a
live Professional account — none is configured for this session.
"""

import pytest
import requests

from src.publishing import hosting, instagram
from src.publishing.types import PublishAuthError, PublishError


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
    """Routes by URL substring so one fake session can stand in for both the
    Graph API and the GitHub hosting calls `publish()` makes internally."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, params=None, headers=None, timeout=None):
        return self._next("get", url, params=params, headers=headers, timeout=timeout)

    def post(self, url, data=None, json=None, params=None, headers=None, timeout=None):
        return self._next("post", url, data=data, json=json, params=params,
                          headers=headers, timeout=timeout)

    def delete(self, url, headers=None, timeout=None):
        return self._next("delete", url, headers=headers, timeout=timeout)


@pytest.fixture()
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\0" * 1000)
    return path


def status(code):
    return FakeResponse(200, {"status_code": code, "status": code})


# --- caption building -----------------------------------------------------


def test_build_caption_joins_title_description_and_hashtags():
    caption = instagram.build_caption("Title", "Desc", ["F1", "Shorts"])
    assert "Title" in caption
    assert "Desc" in caption
    assert "#F1" in caption and "#Shorts" in caption


def test_build_caption_truncates_to_the_limit():
    caption = instagram.build_caption("T" * 3000, "", [], limit=2200)
    assert len(caption) <= 2200
    assert caption.endswith("…")


def test_build_caption_omits_empty_parts():
    caption = instagram.build_caption("Title", "", [])
    assert caption == "Title"


# --- container lifecycle --------------------------------------------------


def test_create_container_returns_the_container_id():
    session = FakeSession([FakeResponse(200, {"id": "CID1"})])
    cid = instagram.create_container("IGUSER", "https://example.com/clip.mp4", "cap", "TOK", session)
    assert cid == "CID1"
    call = session.calls[0]
    assert call["data"]["media_type"] == "REELS"
    assert call["data"]["video_url"] == "https://example.com/clip.mp4"


def test_create_container_raises_auth_error_on_code_190():
    session = FakeSession([FakeResponse(400, {"error": {"code": 190, "message": "bad token"}})])
    with pytest.raises(PublishAuthError):
        instagram.create_container("IGUSER", "url", "cap", "BAD", session)


def test_create_container_raises_generic_error_on_other_failures():
    session = FakeSession([FakeResponse(400, {"error": {"message": "video too long"}})])
    with pytest.raises(PublishError, match="video too long"):
        instagram.create_container("IGUSER", "url", "cap", "TOK", session)


def test_poll_until_ready_returns_once_finished():
    session = FakeSession([status("IN_PROGRESS"), status("IN_PROGRESS"), status("FINISHED")])
    slept = []
    instagram.poll_until_ready("CID1", "TOK", session, sleep=slept.append, poll_interval=5)
    assert slept == [5, 5]


def test_poll_until_ready_treats_already_published_as_ready():
    session = FakeSession([status("PUBLISHED")])
    instagram.poll_until_ready("CID1", "TOK", session, sleep=lambda s: None)  # no raise


def test_poll_until_ready_raises_processing_failed_on_error():
    session = FakeSession([status("ERROR")])
    with pytest.raises(instagram.InstagramProcessingFailed):
        instagram.poll_until_ready("CID1", "TOK", session, sleep=lambda s: None)


def test_poll_until_ready_raises_processing_failed_on_expired():
    session = FakeSession([status("EXPIRED")])
    with pytest.raises(instagram.InstagramProcessingFailed):
        instagram.poll_until_ready("CID1", "TOK", session, sleep=lambda s: None)


def test_poll_until_ready_times_out():
    session = FakeSession([status("IN_PROGRESS")] * 5)
    with pytest.raises(PublishError, match="did not finish"):
        instagram.poll_until_ready(
            "CID1", "TOK", session, sleep=lambda s: None,
            poll_interval=10, poll_timeout=30,
        )


def test_publish_container_returns_the_media_id():
    session = FakeSession([FakeResponse(200, {"id": "MEDIA1"})])
    media_id = instagram.publish_container("IGUSER", "CID1", "TOK", session)
    assert media_id == "MEDIA1"


def test_get_account_info_returns_the_body():
    session = FakeSession([FakeResponse(200, {"id": "IGUSER", "username": "me"})])
    info = instagram.get_account_info("IGUSER", "TOK", session)
    assert info["username"] == "me"


def test_get_account_info_raises_auth_error():
    session = FakeSession([FakeResponse(400, {"error": {"code": 190, "message": "expired"}})])
    with pytest.raises(PublishAuthError):
        instagram.get_account_info("IGUSER", "BAD", session)


# --- full publish(): hosting + container lifecycle, tied together --------


def test_publish_hosts_creates_polls_publishes_then_cleans_up(clip):
    session = FakeSession([
        FakeResponse(200, {"id": 555}),                       # get release
        FakeResponse(201, {"id": 42, "browser_download_url": "https://dl/clip.mp4"}),  # upload asset
        FakeResponse(200, {"id": "CID1"}),                    # create container
        status("FINISHED"),                                   # poll
        FakeResponse(200, {"id": "MEDIA1"}),                  # publish
        FakeResponse(204),                                    # delete asset
    ])
    result = instagram.publish(
        clip, "Title", "Desc", ["F1"], "IGUSER", "TOK",
        "me/repo", "GHTOKEN", session=session, sleep=lambda s: None,
    )
    assert result.media_id == "MEDIA1"

    create_call = session.calls[2]
    assert create_call["data"]["video_url"] == "https://dl/clip.mp4"
    # Cleanup happened: the last call deletes the hosted asset.
    assert session.calls[-1]["method"] == "delete"


def test_publish_cleans_up_the_hosted_asset_even_if_publishing_fails(clip):
    session = FakeSession([
        FakeResponse(200, {"id": 555}),
        FakeResponse(201, {"id": 42, "browser_download_url": "https://dl/clip.mp4"}),
        FakeResponse(200, {"id": "CID1"}),
        status("ERROR"),                                       # poll fails
        FakeResponse(204),                                      # delete asset still happens
    ])
    with pytest.raises(instagram.InstagramProcessingFailed):
        instagram.publish(
            clip, "Title", "Desc", ["F1"], "IGUSER", "TOK",
            "me/repo", "GHTOKEN", session=session, sleep=lambda s: None,
        )
    assert session.calls[-1]["method"] == "delete"


def test_publish_does_not_raise_if_cleanup_itself_fails(clip):
    """The publish succeeded; a cleanup failure must not turn that into an
    error the caller sees as a failed publish."""
    session = FakeSession([
        FakeResponse(200, {"id": 555}),
        FakeResponse(201, {"id": 42, "browser_download_url": "https://dl/clip.mp4"}),
        FakeResponse(200, {"id": "CID1"}),
        status("FINISHED"),
        FakeResponse(200, {"id": "MEDIA1"}),
        FakeResponse(500, text="cleanup boom"),
    ])
    result = instagram.publish(
        clip, "Title", "Desc", ["F1"], "IGUSER", "TOK",
        "me/repo", "GHTOKEN", session=session, sleep=lambda s: None,
    )
    assert result.media_id == "MEDIA1"
