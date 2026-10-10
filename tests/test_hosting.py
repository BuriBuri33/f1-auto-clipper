"""GitHub Release asset hosting — the bridge for Instagram's video_url.

The actual GitHub API was not exercised live (no repo write token configured
for this session); shapes below follow GitHub's documented REST responses
for releases and release assets.
"""

import pytest
import requests

from src.publishing import hosting
from src.publishing.types import PublishAuthError


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

    def _next(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, headers=None, timeout=None):
        return self._next("get", url, headers=headers, timeout=timeout)

    def post(self, url, headers=None, json=None, params=None, data=None, timeout=None):
        return self._next("post", url, headers=headers, json=json, params=params,
                          data=data, timeout=timeout)

    def delete(self, url, headers=None, timeout=None):
        return self._next("delete", url, headers=headers, timeout=timeout)


@pytest.fixture()
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\0" * 1000)
    return path


RELEASE_FOUND = FakeResponse(200, {"id": 555})
RELEASE_NOT_FOUND = FakeResponse(404, {"message": "Not Found"})
RELEASE_CREATED = FakeResponse(201, {"id": 777})
ASSET_UPLOADED = FakeResponse(201, {
    "id": 42, "browser_download_url": "https://github.com/me/repo/releases/download/clip-hosting/clip.mp4",
})


def test_upload_asset_reuses_an_existing_release(clip):
    session = FakeSession([RELEASE_FOUND, ASSET_UPLOADED])
    asset = hosting.upload_asset("me/repo", clip, "TOKEN", session=session)
    assert asset.asset_id == 42
    assert asset.url.endswith("clip.mp4")
    # Only 2 calls: no release-creation call was made.
    assert len(session.calls) == 2


def test_upload_asset_creates_the_release_if_missing(clip):
    session = FakeSession([RELEASE_NOT_FOUND, RELEASE_CREATED, ASSET_UPLOADED])
    asset = hosting.upload_asset("me/repo", clip, "TOKEN", session=session)
    assert asset.asset_id == 42
    create_call = session.calls[1]
    assert create_call["json"]["tag_name"] == "clip-hosting"
    assert create_call["json"]["prerelease"] is True


def test_upload_asset_raises_on_a_lookup_error_other_than_404(clip):
    session = FakeSession([FakeResponse(500, text="boom")])
    with pytest.raises(hosting.HostingError):
        hosting.upload_asset("me/repo", clip, "TOKEN", session=session)


def test_upload_asset_raises_auth_error_on_403():
    session = FakeSession([FakeResponse(403, text="forbidden")])
    with pytest.raises(PublishAuthError):
        hosting._get_or_create_release("me/repo", "clip-hosting", "BAD", session, 10)


def test_upload_asset_raises_if_the_file_does_not_exist(tmp_path):
    with pytest.raises(hosting.HostingError, match="does not exist"):
        hosting.upload_asset("me/repo", tmp_path / "nope.mp4", "TOKEN", session=FakeSession([]))


def test_upload_asset_raises_on_upload_failure(clip):
    session = FakeSession([RELEASE_FOUND, FakeResponse(422, text="unprocessable")])
    with pytest.raises(hosting.HostingError, match="422"):
        hosting.upload_asset("me/repo", clip, "TOKEN", session=session)


def test_delete_asset_succeeds_on_204():
    session = FakeSession([FakeResponse(204)])
    hosting.delete_asset("me/repo", 42, "TOKEN", session=session)  # no raise


def test_delete_asset_is_a_no_op_on_already_gone():
    session = FakeSession([FakeResponse(404)])
    hosting.delete_asset("me/repo", 42, "TOKEN", session=session)  # no raise


def test_delete_asset_raises_on_a_real_failure():
    session = FakeSession([FakeResponse(500, text="boom")])
    with pytest.raises(hosting.HostingError):
        hosting.delete_asset("me/repo", 42, "TOKEN", session=session)


def test_a_connection_error_is_wrapped_as_hosting_error(clip):
    session = FakeSession([requests.ConnectionError("dns failure")])
    with pytest.raises(hosting.HostingError, match="dns failure"):
        hosting.upload_asset("me/repo", clip, "TOKEN", session=session)
