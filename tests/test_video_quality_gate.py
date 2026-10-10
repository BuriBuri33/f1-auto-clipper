from pathlib import Path

from src.video import quality_gate


def good_probe(width=1080, height=1920, fps="30/1", vcodec="h264", acodec="aac", duration="45.0"):
    return {
        "streams": [
            {"codec_type": "video", "codec_name": vcodec, "width": width,
             "height": height, "r_frame_rate": fps},
            {"codec_type": "audio", "codec_name": acodec},
        ],
        "format": {"duration": duration},
    }


def make_file(tmp_path, size=100_000):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\0" * size)
    return path


def check(tmp_path, probe_data, **overrides):
    defaults = dict(
        expected_width=1080, expected_height=1920, expected_fps=30.0,
        expected_video_codec="h264", expected_audio_codec="aac",
        expected_duration=45.0,
    )
    defaults.update(overrides)
    return quality_gate.check(make_file(tmp_path), probe_data, **defaults)


def test_a_conforming_file_passes(tmp_path):
    result = check(tmp_path, good_probe())
    assert result.passed
    assert result.failures == []


def test_missing_file_fails_immediately(tmp_path):
    result = quality_gate.check(
        tmp_path / "nope.mp4", good_probe(),
        expected_width=1080, expected_height=1920, expected_fps=30.0,
        expected_video_codec="h264", expected_audio_codec="aac",
        expected_duration=45.0,
    )
    assert not result.passed
    assert "does not exist" in result.reason


def test_a_near_empty_file_fails_the_plausibility_floor(tmp_path):
    path = make_file(tmp_path, size=10)
    result = quality_gate.check(
        path, good_probe(),
        expected_width=1080, expected_height=1920, expected_fps=30.0,
        expected_video_codec="h264", expected_audio_codec="aac",
        expected_duration=45.0,
    )
    assert not result.passed
    assert "byte plausibility floor" in result.reason


def test_wrong_resolution_fails(tmp_path):
    result = check(tmp_path, good_probe(width=640, height=360))
    assert not result.passed
    assert "resolution" in result.reason


def test_wrong_video_codec_fails(tmp_path):
    result = check(tmp_path, good_probe(vcodec="vp9"))
    assert not result.passed
    assert "video codec" in result.reason


def test_wrong_audio_codec_fails(tmp_path):
    result = check(tmp_path, good_probe(acodec="opus"))
    assert not result.passed
    assert "audio codec" in result.reason


def test_fps_outside_tolerance_fails(tmp_path):
    result = check(tmp_path, good_probe(fps="24/1"))
    assert not result.passed
    assert "frame rate" in result.reason


def test_fps_within_tolerance_passes():
    assert quality_gate._parse_r_frame_rate("30000/1001") == 30000 / 1001


def test_duration_outside_tolerance_fails(tmp_path):
    result = check(tmp_path, good_probe(duration="20.0"))
    assert not result.passed
    assert "duration" in result.reason


def test_duration_within_tolerance_passes(tmp_path):
    result = check(tmp_path, good_probe(duration="45.6"))
    assert result.passed


def test_missing_audio_stream_fails(tmp_path):
    probe_data = good_probe()
    probe_data["streams"] = [s for s in probe_data["streams"] if s["codec_type"] != "audio"]
    result = check(tmp_path, probe_data)
    assert not result.passed
    assert "audio stream" in result.reason


def test_multiple_failures_are_all_reported_at_once(tmp_path):
    result = check(tmp_path, good_probe(width=100, height=100, vcodec="vp9"))
    assert not result.passed
    assert "resolution" in result.reason
    assert "video codec" in result.reason
