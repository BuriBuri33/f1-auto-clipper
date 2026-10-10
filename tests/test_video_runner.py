"""Render pass orchestration.

FFmpeg itself is faked here (`render_clip` and `ffmpeg.probe` are
monkeypatched) — these tests are about status transitions, the
best-candidate-then-backups order, and the environment pre-check, not about
what FFmpeg actually produces. The filter-string/ASS/quality-gate LOGIC has
its own unit tests; a real end-to-end encode is covered separately
(test_integration_render.py, skipped where the required filters are absent).
"""

from pathlib import Path

import pytest

from src.database import db
from src.download import downloader
from src.transcription.types import Segment, Transcript
from src.utils.config import Config, MonitoringConfig, Source
from src.utils.logging import get_logger
from src.video import runner as render_runner
from src.video.types import RenderError, RenderMetadata, RenderResult, ReframeDecision
from tests.fakes_ytdlp import REAL_INFO, factory, writes_a_file


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def environment_ready(monkeypatch):
    monkeypatch.setattr(downloader, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(render_runner.ffmpeg, "missing_required_filters", lambda: [])


def make_config():
    return Config(
        database_path=":memory:",
        monitoring=MonitoringConfig(max_retries=1, backoff_base_seconds=0),
        raw={
            "video": {"width": 1080, "height": 1920, "fps": 30, "video_codec": "libx264",
                      "audio_codec": "aac", "reframe_strategy": "center_crop",
                      "crf": 20, "preset": "medium", "timeout_seconds": 300,
                      "max_videos_per_run": 3, "max_retries": 3},
            "captions": {"enabled": True, "max_chars_per_cue": 36,
                        "max_seconds_per_cue": 4.0, "pause_threshold_seconds": 0.5,
                        "margin_bottom": 220, "margin_side": 80},
            "branding": {"enabled": False, "watermark_text": "", "position": "top_right"},
            "clip": {"max_title_chars": 95, "max_hashtags": 6},
            "download": {"player_clients": ["default"], "preferred_max_height": 1440,
                        "work_dir": ""},
            "analysis": {"vocabulary_hint": ""},
        },
    )


def seed(conn, tmp_path, video_id="VID1", candidates=((0, 20000, 90.0),)):
    source = Source("s", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    sid = db.sync_sources(conn, [source])["s"]
    _, vid = db.record_discovered_video(
        conn, sid, video_id, "A video",
        "https://www.youtube.com/watch?v=%s" % video_id, None,
    )
    transcript = Transcript(
        segments=[Segment(0.0, 20.0, "Box box box.")],
        language="en", backend="local", model="tiny", source_duration=600.0,
    )
    tpath = tmp_path / ("%s.json" % video_id)
    transcript.write(tpath)
    conn.execute(
        "UPDATE source_videos SET transcript_path = ?, status = ? WHERE id = ?",
        (str(tpath), "ANALYZED", vid),
    )
    conn.commit()
    clip_ids = []
    for start_ms, end_ms, score in candidates:
        cur = conn.execute(
            "INSERT INTO clips (source_video_id, start_ms, end_ms, score, reason, "
            " hook, status, created_at) VALUES (?, ?, ?, ?, 'r', 'h', 'CANDIDATE', ?)",
            (vid, start_ms, end_ms, score, db.utcnow()),
        )
        clip_ids.append(cur.lastrowid)
    conn.commit()
    return vid, clip_ids


GOOD_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1080,
         "height": 1920, "r_frame_rate": "30/1"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {"duration": "20.0"},
}
BAD_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 100,
         "height": 100, "r_frame_rate": "30/1"},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
    "format": {"duration": "20.0"},
}


def fake_render_result(path):
    return RenderResult(
        path=path, duration_seconds=20.0, width=1080, height=1920, fps=30.0,
        video_codec="libx264", audio_codec="aac",
        reframe=ReframeDecision("center_crop", "configured"), caption_cue_count=1,
        metadata=RenderMetadata(title="T", description="D", hashtags=["F1"]),
    )


def install_fakes(monkeypatch, outcomes):
    """outcomes: {clip_id: "ok" | "ffmpeg_error" | "bad_gate"}"""
    probes = {}

    def fake_render_clip(source_media_path, clip_start, clip_end, transcript,
                         source_title, source_url, reason, hook, out_dir, out_name,
                         video_cfg, captions_cfg, branding_cfg, vocabulary_hint="",
                         max_title_chars=95, max_hashtags=6):
        clip_id = int(out_name.rsplit("_", 1)[1])
        outcome = outcomes.get(clip_id, "ok")
        if outcome == "ffmpeg_error":
            raise RenderError("ffmpeg exploded")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / (out_name + ".mp4")
        path.write_bytes(b"\0" * 60_000)  # over quality_gate.MIN_PLAUSIBLE_BYTES
        probes[str(path)] = BAD_PROBE if outcome == "bad_gate" else GOOD_PROBE
        return fake_render_result(path)

    def fake_probe(path):
        return probes[str(path)]

    monkeypatch.setattr(render_runner, "render_clip", fake_render_clip)
    monkeypatch.setattr(render_runner.ffmpeg, "probe", fake_probe)


def writes_media(dest, info):
    path = Path(dest) / ("%s.mp4" % info["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 4096)
    return path


def run(conn, tmp_path, config=None, ydl_factory=None):
    return render_runner.run_render(
        conn, config or make_config(), work_dir=tmp_path / "work",
        log=get_logger("test"),
        ydl_factory=ydl_factory or factory({"default": REAL_INFO}, write_file=writes_media),
    )


# --- happy path --------------------------------------------------------


def test_the_best_candidate_is_tried_first_and_rendered(conn, tmp_path, monkeypatch):
    vid, (best, backup) = seed(
        conn, tmp_path, candidates=((0, 20000, 90.0), (30000, 50000, 40.0)),
    )
    install_fakes(monkeypatch, {best: "ok"})

    result = run(conn, tmp_path)

    assert result.rendered == 1
    outcome = result.outcomes[0]
    assert outcome.status == "RENDERED"
    assert outcome.clip_id == best
    assert len(outcome.attempts) == 1

    clip_row = conn.execute("SELECT * FROM clips WHERE id = ?", (best,)).fetchone()
    assert clip_row["status"] == "RENDERED"
    assert clip_row["title"] == "T"
    assert clip_row["hashtags"] == "F1"

    video_row = db.get_video(conn, "VID1")
    assert video_row["status"] == "RENDERED"


def test_a_rejected_candidate_falls_through_to_the_backup(conn, tmp_path, monkeypatch):
    vid, (best, backup) = seed(
        conn, tmp_path, candidates=((0, 20000, 90.0), (30000, 50000, 40.0)),
    )
    install_fakes(monkeypatch, {best: "bad_gate", backup: "ok"})

    result = run(conn, tmp_path)

    assert result.rendered == 1
    assert result.outcomes[0].clip_id == backup
    assert len(result.outcomes[0].attempts) == 2

    best_row = conn.execute("SELECT * FROM clips WHERE id = ?", (best,)).fetchone()
    assert best_row["status"] == "REJECTED"
    assert "resolution" in best_row["status_detail"]


def test_an_ffmpeg_failure_also_falls_through_to_the_backup(conn, tmp_path, monkeypatch):
    vid, (best, backup) = seed(
        conn, tmp_path, candidates=((0, 20000, 90.0), (30000, 50000, 40.0)),
    )
    install_fakes(monkeypatch, {best: "ffmpeg_error", backup: "ok"})

    result = run(conn, tmp_path)

    assert result.rendered == 1
    best_row = conn.execute("SELECT * FROM clips WHERE id = ?", (best,)).fetchone()
    assert best_row["status"] == "REJECTED"
    assert "FFmpeg render failed" in best_row["status_detail"]


def test_every_candidate_rejected_fails_the_video_and_bumps_retry(conn, tmp_path, monkeypatch):
    vid, (best, backup) = seed(
        conn, tmp_path, candidates=((0, 20000, 90.0), (30000, 50000, 40.0)),
    )
    install_fakes(monkeypatch, {best: "bad_gate", backup: "bad_gate"})

    result = run(conn, tmp_path)

    assert result.rendered == 0
    assert len(result.failed) == 1
    video_row = db.get_video(conn, "VID1")
    assert video_row["status"] == "FAILED"
    assert video_row["retry_count"] == 1
    for cid in (best, backup):
        row = conn.execute("SELECT status FROM clips WHERE id = ?", (cid,)).fetchone()
        assert row["status"] == "REJECTED"


# --- re-download failures -----------------------------------------------


def test_source_video_now_unavailable_is_skipped_terminally(conn, tmp_path, monkeypatch):
    seed(conn, tmp_path)

    def boom(*a, **kw):
        raise downloader.UnavailableVideo("gone")

    monkeypatch.setattr(downloader, "download", boom)
    result = run(conn, tmp_path)

    assert result.outcomes[0].status == "SKIPPED"
    assert db.get_video(conn, "VID1")["status"] == "SKIPPED"


def test_a_transient_download_failure_fails_and_bumps_retry(conn, tmp_path, monkeypatch):
    seed(conn, tmp_path)

    def boom(*a, **kw):
        raise downloader.DownloadError("network hiccup")

    monkeypatch.setattr(downloader, "download", boom)
    result = run(conn, tmp_path)

    assert result.outcomes[0].status == "FAILED"
    row = db.get_video(conn, "VID1")
    assert row["status"] == "FAILED"
    assert row["retry_count"] == 1


# --- preconditions --------------------------------------------------------


def test_missing_transcript_file_fails_the_video(conn, tmp_path, monkeypatch):
    vid, _ = seed(conn, tmp_path)
    conn.execute(
        "UPDATE source_videos SET transcript_path = ? WHERE id = ?",
        (str(tmp_path / "does-not-exist.json"), vid),
    )
    conn.commit()
    install_fakes(monkeypatch, {})

    result = run(conn, tmp_path)
    assert result.outcomes[0].status == "FAILED"
    assert "transcript file missing" in result.outcomes[0].reason


def test_a_video_marked_analyzed_with_no_candidate_clips_fails_cleanly(conn, tmp_path):
    vid, (clip_id,) = seed(conn, tmp_path)
    conn.execute("UPDATE clips SET status = 'REJECTED' WHERE id = ?", (clip_id,))
    conn.commit()

    result = run(conn, tmp_path)
    assert result.outcomes[0].status == "FAILED"
    assert "no CANDIDATE clips" in result.outcomes[0].reason


# --- the environment pre-check -------------------------------------------


def test_run_render_refuses_to_touch_any_video_when_filters_are_missing(
    conn, tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        render_runner.ffmpeg, "missing_required_filters", lambda: ["ass", "drawtext"]
    )
    vid, _ = seed(conn, tmp_path)

    result = run(conn, tmp_path)

    assert result.environment_error is not None
    assert "ass" in result.environment_error
    assert result.outcomes == []
    # Nothing was touched — still ANALYZED, not FAILED.
    assert db.get_video(conn, "VID1")["status"] == "ANALYZED"
