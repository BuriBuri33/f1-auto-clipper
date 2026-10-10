import pytest

from src.download import diagnose, downloader
from tests.fakes_ytdlp import REAL_INFO, factory, writes_a_file


def names(report):
    return {c.name: c for c in report.checks}


def test_a_healthy_environment_reports_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    report = diagnose.run_diagnostics(
        api_key="KEY", dest_dir=tmp_path,
        ydl_factory=factory({"default": REAL_INFO}, write_file=writes_a_file()),
        session=FakeSession([key_response()]),
    )
    assert report.ok is True
    assert names(report)["fetch media bytes"].ok is True
    assert names(report)["YouTube Data API key works"].ok is True


def test_a_missing_api_key_is_reported_but_does_not_stop_the_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    report = diagnose.run_diagnostics(
        api_key=None, dest_dir=tmp_path,
        ydl_factory=factory({"default": REAL_INFO}, write_file=writes_a_file()),
    )
    checks = names(report)
    assert checks["YouTube Data API key works"].ok is False
    assert checks["fetch media bytes"].ok is True        # still checked
    assert report.ok is False


def test_missing_ffmpeg_is_reported_with_install_instructions(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: None)
    report = diagnose.run_diagnostics(
        api_key="KEY", dest_dir=tmp_path,
        ydl_factory=factory({"default": REAL_INFO}, write_file=writes_a_file()),
    )
    detail = names(report)["FFmpeg on PATH"].detail
    assert "brew install ffmpeg" in detail
    assert "apt-get install" in detail


def test_a_failed_probe_stops_before_attempting_a_download(tmp_path, monkeypatch):
    """No point fetching bytes for a video we cannot even describe."""
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    report = diagnose.run_diagnostics(
        api_key="KEY", dest_dir=tmp_path, ydl_factory=factory({}),
    )
    checks = names(report)
    assert checks["probe a public video"].ok is False
    assert "fetch media bytes" not in checks


def test_the_real_observed_failure_is_reported_with_every_client(tmp_path, monkeypatch):
    """The exact shape of the 2026-10-10 failure this command exists for."""
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    script = {
        "default": RuntimeError("The page needs to be reloaded."),
        "android_vr": REAL_INFO,          # probes fine...
    }

    calls = []
    probing = factory(script, calls=calls)

    def download_always_403(opts):
        inner = probing(opts)
        original = inner.extract_info

        def extract(url, download=False):
            info = original(url, download=download)
            if download:
                raise RuntimeError("unable to download video data: HTTP Error 403: Forbidden")
            return info

        inner.extract_info = extract
        return inner

    report = diagnose.run_diagnostics(
        api_key="KEY", dest_dir=tmp_path,
        player_clients=("default", "android_vr"),
        ydl_factory=download_always_403,
    )
    checks = names(report)
    assert checks["probe a public video"].ok is True      # ...but bytes fail
    assert checks["fetch media bytes"].ok is False
    assert "403" in checks["fetch media bytes"].detail
    assert report.ok is False


def test_no_download_mode_skips_the_byte_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    report = diagnose.run_diagnostics(
        api_key="KEY", do_download=False, dest_dir=tmp_path,
        ydl_factory=factory({"default": REAL_INFO}),
        session=FakeSession([key_response()]),
    )
    assert names(report)["fetch media bytes"].detail == "skipped (--no-download)"
    assert report.ok is True


def test_format_report_marks_each_check_and_gives_a_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: None)
    report = diagnose.run_diagnostics(
        api_key=None, do_download=False, dest_dir=tmp_path,
        ydl_factory=factory({"default": REAL_INFO}),
    )
    text = diagnose.format_report(report)
    assert "[PASS]" in text and "[FAIL]" in text
    assert "Overall: NOT ready" in text


# --- API key validation ----------------------------------------------------

from src.monitoring import youtube_api  # noqa: E402
from tests.test_youtube_api import FakeResponse, FakeSession  # noqa: E402


def key_response(license="youtube", duration="PT9M57S"):
    return FakeResponse(200, {"items": [{
        "id": diagnose.PROBE_TARGET_ID,
        "snippet": {"title": "t", "liveBroadcastContent": "none"},
        "contentDetails": {"duration": duration},
        "status": {"license": license, "privacyStatus": "public"},
    }]})


def key_check(session, api_key="KEY", monkeypatch=None, tmp_path=None):
    return diagnose.run_diagnostics(
        api_key=api_key, do_download=False, dest_dir=tmp_path,
        ydl_factory=factory({"default": REAL_INFO}), session=session,
    )


def test_a_working_key_passes_whatever_the_licence_value_is(tmp_path, monkeypatch):
    """The licence is a third party's setting; asserting it would make our own
    health check fail because of someone else's edit."""
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    for value in ("youtube", "creativeCommon"):
        report = key_check(FakeSession([key_response(license=value)]), tmp_path=tmp_path)
        check = names(report)["YouTube Data API key works"]
        assert check.ok is True, value
        assert repr(value) in check.detail


def test_an_unrecognised_licence_value_fails_because_the_gate_compares_exactly(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    report = key_check(FakeSession([key_response(license="somethingNew")]),
                       tmp_path=tmp_path)
    check = names(report)["YouTube Data API key works"]
    assert check.ok is False
    assert "treated as restricted" in check.detail


def test_a_rejected_key_reports_the_three_usual_causes(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    payload = {"error": {"message": "API key not valid"}}
    report = key_check(FakeSession([FakeResponse(400, payload)]), tmp_path=tmp_path)
    detail = names(report)["YouTube Data API key works"].detail
    assert "not enabled" in detail          # API not enabled on the project
    assert "restrictions" in detail         # key restrictions


def test_a_spent_quota_is_distinguished_from_a_bad_key(tmp_path, monkeypatch):
    """Very different actions: wait vs fix the key."""
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    payload = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    report = key_check(FakeSession([FakeResponse(403, payload)]), tmp_path=tmp_path)
    detail = names(report)["YouTube Data API key works"].detail
    assert "the key is valid" in detail
    assert "midnight Pacific" in detail


def test_a_missing_key_explains_the_consequence_not_just_the_absence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    report = key_check(FakeSession([]), api_key=None, tmp_path=tmp_path)
    detail = names(report)["YouTube Data API key works"].detail
    assert "nothing will ever download" in detail
    assert ".env" in detail


def test_an_empty_items_list_is_not_read_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    report = key_check(FakeSession([FakeResponse(200, {"items": []})]), tmp_path=tmp_path)
    assert names(report)["YouTube Data API key works"].ok is False


# --- yt-dlp version floor --------------------------------------------------
#
# This floor is the cheapest fix for the most expensive misdiagnosis in the
# project: a stale yt-dlp fails with 403s and "page needs to be reloaded",
# which is indistinguishable from being IP-blocked. Measured 2026-10-10:
# 2025.10.14 failed on all 9 clients, 2026.08.19 succeeded on the first.


@pytest.mark.parametrize("version,expected", [
    ("2025.10.14", True),       # the version that could not download
    ("2026.08.18", True),
    ("2026.08.19", False),      # the oldest measured working version
    ("2026.08.20", False),
    ("2026.09.01", False),
    ("2027.01.01", False),
    ("2024.12.23", True),
])
def test_version_floor_comparison(version, expected):
    assert diagnose._ytdlp_is_too_old(version) is expected


def test_a_zero_padded_month_does_not_parse_as_octal():
    """2026.08.19 must compare as (2026, 8, 19), not fail or misread."""
    assert diagnose._parse_ytdlp_version("2026.08.19") == (2026, 8, 19)


def test_a_version_with_a_suffix_still_parses():
    assert diagnose._parse_ytdlp_version("2026.08.19.1") == (2026, 8, 19)
    assert diagnose._parse_ytdlp_version("2026.08.19rc1") == (2026, 8, 19)


@pytest.mark.parametrize("version", ["unknown", "", "dev", "master", "x.y.z"])
def test_an_unparseable_version_is_not_condemned(version):
    """A nightly or a fork must not be reported broken on its version string;
    the byte-fetch check is the real evidence either way."""
    assert diagnose._parse_ytdlp_version(version) is None
    assert diagnose._ytdlp_is_too_old(version) is False


def test_the_report_names_both_upgrade_routes_when_too_old(tmp_path, monkeypatch):
    """Including the git route, because a stale package index was the actual
    cause here and `pip install --upgrade yt-dlp` alone could not fix it."""
    monkeypatch.setattr(diagnose, "_ytdlp_is_too_old", lambda _: True)
    monkeypatch.setattr(diagnose.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    report = diagnose.run_diagnostics(
        api_key="KEY", do_download=False, dest_dir=tmp_path,
        ydl_factory=factory({"default": REAL_INFO}),
        session=FakeSession([key_response()]),
    )
    check = names(report)["yt-dlp new enough"]
    assert check.ok is False
    assert "pip install --upgrade yt-dlp" in check.detail
    assert "git+https://github.com/yt-dlp/yt-dlp.git" in check.detail
    assert "looks exactly like" in check.detail      # names the misdiagnosis
    assert report.ok is False
