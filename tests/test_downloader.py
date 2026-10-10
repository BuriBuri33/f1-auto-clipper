import pytest

from src.download import downloader
from tests.fakes_ytdlp import REAL_INFO, factory, writes_a_file, writes_nothing


# --- format selection ------------------------------------------------------


def test_format_selector_prefers_h264_and_aac_in_mp4():
    selector = downloader.format_selector(1440, allow_progressive_only=False)
    assert selector.startswith("bestvideo[height<=1440][vcodec^=avc1][ext=mp4]+")
    assert "bestaudio[acodec^=mp4a][ext=m4a]" in selector


def test_format_selector_honours_the_height_cap():
    assert "height<=2160" in downloader.format_selector(2160, False)


def test_progressive_only_selector_asks_for_single_file_formats():
    selector = downloader.format_selector(1440, allow_progressive_only=True)
    assert "bestvideo" not in selector
    assert "vcodec!=none][acodec!=none" in selector


# --- error classification --------------------------------------------------


@pytest.mark.parametrize("message", [
    "ERROR: [youtube] X: The page needs to be reloaded.",
    "Requested format is not available",
    "Unable to extract player response",
    "Sign in to confirm you're not a bot",
    "HTTP Error 403: Forbidden",
])
def test_transient_extractor_errors_are_classified_transient(message):
    assert downloader._classify(RuntimeError(message)) == "transient"


@pytest.mark.parametrize("message", [
    "Video unavailable",
    "Private video. Sign in if you've been granted access",
    "This video has been removed by the uploader",
    "Join this channel to get access to members-only content",
    "The video is age restricted",
])
def test_permanent_extractor_errors_are_classified_permanent(message):
    assert downloader._classify(RuntimeError(message)) == "permanent"


# --- probe -----------------------------------------------------------------


def test_probe_reads_the_real_metadata_shape():
    ydl = factory({"default": REAL_INFO})
    result = downloader.probe("https://x/watch?v=YE7VzlLtp-4", ydl_factory=ydl)

    assert result.video_id == "YE7VzlLtp-4"
    assert result.duration_seconds == 597
    assert result.estimated_bytes == 25337037
    assert result.live_status == "not_live"
    assert result.is_live is False
    assert result.player_client == "default"


def test_probe_never_downloads():
    ydl = factory({"default": REAL_INFO})
    downloader.probe("https://x", ydl_factory=ydl)
    assert all(call["download"] is False for call in ydl.calls)
    assert all(call["opts"].get("skip_download") for call in ydl.calls)


def test_probe_walks_the_client_chain_until_one_works():
    """The observed real-world case: the default client is broken, a later
    one works."""
    ydl = factory({
        "default": RuntimeError("The page needs to be reloaded."),
        "android_vr": REAL_INFO,
    })
    result = downloader.probe(
        "https://x", player_clients=("default", "android_vr", "tv"), ydl_factory=ydl
    )
    assert result.player_client == "android_vr"
    assert [c["client"] for c in ydl.calls] == ["default", "android_vr"]


def test_unanimous_unavailable_is_terminal():
    """Every client agrees it is gone, so it is gone."""
    ydl = factory({
        "default": RuntimeError("Video unavailable"),
        "android_vr": RuntimeError("This video is unavailable"),
        "tv": RuntimeError("Private video"),
    })
    with pytest.raises(downloader.UnavailableVideo):
        downloader.probe(
            "https://x", player_clients=("default", "android_vr", "tv"), ydl_factory=ydl
        )
    assert len(ydl.calls) == 3


def test_one_client_crying_unavailable_does_not_condemn_the_video():
    """The observed real case: `mweb` reported "Video unavailable" for a video
    `android_vr` probed successfully in the same second. A single permanent-
    looking error must not end as a terminal SKIP."""
    ydl = factory({
        "default": RuntimeError("The page needs to be reloaded."),
        "mweb": RuntimeError("Video unavailable"),
        "android_vr": REAL_INFO,
    })
    result = downloader.probe(
        "https://x", player_clients=("default", "mweb", "android_vr"), ydl_factory=ydl
    )
    assert result.player_client == "android_vr"


def test_a_mixed_chain_fails_retryably_not_terminally():
    """Mixed signals mean 'we do not know', and not-knowing must stay retryable."""
    ydl = factory({
        "default": RuntimeError("Video unavailable"),
        "android_vr": RuntimeError("HTTP Error 403: Forbidden"),
    })
    with pytest.raises(downloader.DownloadError) as exc:
        downloader.probe(
            "https://x", player_clients=("default", "android_vr"), ydl_factory=ydl
        )
    assert not isinstance(exc.value, downloader.UnavailableVideo)


def test_the_failure_message_names_every_client_and_its_classification():
    ydl = factory({
        "default": RuntimeError("Video unavailable"),
        "tv": RuntimeError("HTTP Error 403: Forbidden"),
    })
    with pytest.raises(downloader.DownloadError) as exc:
        downloader.probe("https://x", player_clients=("default", "tv"), ydl_factory=ydl)
    message = str(exc.value)
    assert "default [permanent]" in message
    assert "tv [transient]" in message


def test_probe_reports_every_client_it_tried_when_all_fail():
    ydl = factory({})
    with pytest.raises(downloader.DownloadError) as exc:
        downloader.probe("https://x", player_clients=("default", "tv"), ydl_factory=ydl)
    assert "default" in str(exc.value) and "tv" in str(exc.value)


def test_download_chain_unanimity_rule_matches_the_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    unanimous = factory({
        "default": RuntimeError("Video unavailable"),
        "tv": RuntimeError("Private video"),
    })
    with pytest.raises(downloader.UnavailableVideo):
        downloader.download("https://x", dest_dir=tmp_path,
                            player_clients=("default", "tv"), ydl_factory=unanimous)

    mixed = factory({
        "default": RuntimeError("Video unavailable"),
        "tv": RuntimeError("The page needs to be reloaded."),
    })
    with pytest.raises(downloader.DownloadError) as exc:
        downloader.download("https://x", dest_dir=tmp_path,
                            player_clients=("default", "tv"), ydl_factory=mixed)
    assert not isinstance(exc.value, downloader.UnavailableVideo)


def test_the_real_observed_download_failure_chain_is_retryable(tmp_path, monkeypatch):
    """The exact messages collected from YouTube on 2026-10-10. Not one video
    actually gone, so the pass must end FAILED (retryable), never SKIPPED."""
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    observed = factory({
        "default": RuntimeError("ERROR: [youtube] X: The page needs to be reloaded.."),
        "android_vr": RuntimeError("ERROR: unable to download video data: HTTP Error 403: Forbidden"),
        "tv": RuntimeError("ERROR: [youtube] X: The page needs to be reloaded."),
        "web_safari": RuntimeError("ERROR: [youtube] X: The page needs to be reloaded.."),
        "mweb": RuntimeError("ERROR: [youtube] X: Video unavailable"),
        "ios": RuntimeError("ERROR: [youtube] X: Requested format is not available."),
    })
    clients = ("default", "android_vr", "tv", "web_safari", "mweb", "ios")
    with pytest.raises(downloader.DownloadError) as exc:
        downloader.download("https://x", dest_dir=tmp_path,
                            player_clients=clients, ydl_factory=observed)
    assert not isinstance(exc.value, downloader.UnavailableVideo)
    assert len(observed.calls) == 6


def test_probe_treats_an_empty_extractor_result_as_a_failure():
    ydl = factory({"default": {}, "tv": REAL_INFO})
    result = downloader.probe("https://x", player_clients=("default", "tv"), ydl_factory=ydl)
    assert result.player_client == "tv"


def test_probe_surfaces_a_live_stream():
    info = dict(REAL_INFO, live_status="is_live", duration=None)
    result = downloader.probe("https://x", ydl_factory=factory({"default": info}))
    assert result.is_live is True
    assert result.duration_seconds is None


# --- download --------------------------------------------------------------


def test_download_writes_a_file_and_reports_its_size(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file(4096))
    result = downloader.download("https://x", dest_dir=tmp_path, ydl_factory=ydl)

    assert result.path.exists()
    assert result.size_bytes == 4096
    assert result.path.parent == tmp_path


def test_download_requires_ffmpeg_and_says_how_to_get_it(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: False)
    with pytest.raises(downloader.DownloadError) as exc:
        downloader.download("https://x", dest_dir=tmp_path,
                            ydl_factory=factory({"default": REAL_INFO}))
    message = str(exc.value)
    assert "brew install ffmpeg" in message
    assert "apt-get install" in message


def test_progressive_only_mode_does_not_require_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: False)
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file())
    result = downloader.download(
        "https://x", dest_dir=tmp_path, allow_progressive_only=True, ydl_factory=ydl
    )
    assert result.path.exists()


def test_download_passes_the_size_ceiling_to_ytdlp(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file())
    downloader.download("https://x", dest_dir=tmp_path, max_bytes=12345,
                        ydl_factory=ydl)
    assert ydl.calls[0]["opts"]["max_filesize"] == 12345


def test_success_with_no_file_on_disk_is_a_failure(tmp_path, monkeypatch):
    """yt-dlp reports success when max_filesize aborts a download mid-way."""
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    ydl = factory({"default": REAL_INFO}, write_file=writes_nothing)
    with pytest.raises(downloader.DownloadError, match="size ceiling"):
        downloader.download("https://x", dest_dir=tmp_path,
                            player_clients=("default",), ydl_factory=ydl)


def test_download_falls_back_through_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    ydl = factory(
        {"default": RuntimeError("The page needs to be reloaded."),
         "android_vr": REAL_INFO},
        write_file=writes_a_file(),
    )
    result = downloader.download(
        "https://x", dest_dir=tmp_path,
        player_clients=("default", "android_vr"), ydl_factory=ydl,
    )
    assert result.player_client == "android_vr"


def test_output_path_is_found_by_scanning_when_ytdlp_reports_no_path(
    tmp_path, monkeypatch
):
    """The real extension often differs from the one in outtmpl after a merge."""
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)

    def write_without_reporting(dest, info):
        from pathlib import Path
        p = Path(dest) / ("%s.mkv" % info["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 999)
        return None       # deliberately does not report the path

    ydl = factory({"default": REAL_INFO}, write_file=write_without_reporting)
    result = downloader.download("https://x", dest_dir=tmp_path, ydl_factory=ydl)
    assert result.path.name == "YE7VzlLtp-4.mkv"
    assert result.size_bytes == 999


def test_zero_byte_output_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    ydl = factory({"default": REAL_INFO}, write_file=writes_a_file(size_bytes=0))
    with pytest.raises(downloader.DownloadError):
        downloader.download("https://x", dest_dir=tmp_path,
                            player_clients=("default",), ydl_factory=ydl)
