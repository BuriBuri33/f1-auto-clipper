"""Publish pass orchestration: render (faked) + per-platform upload (faked).

Render itself is exercised in tests/test_video_runner.py; here `render_one_video`
is monkeypatched so these tests are purely about what happens to a successful
(or unsuccessful) render afterwards — platform independence, idempotent
skipping, and the all-platforms-failed path.
"""

from pathlib import Path

import pytest

from src.database import db
from src.publishing import runner as publish_runner
from src.publishing.types import PublishAuthError, PublishError
from src.utils.config import Config, MonitoringConfig, Source
from src.utils.logging import get_logger
from src.video import runner as render_runner


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def environment_ready(monkeypatch):
    monkeypatch.setattr(publish_runner.video_ffmpeg, "missing_required_filters", lambda: [])
    # Instagram's publish() hosts the clip via a GitHub Release asset, which
    # needs to know which repo it's running in — normally GITHUB_REPOSITORY,
    # set automatically in Actions.
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")


def make_config(**publishing_overrides):
    publishing = {
        "youtube": True, "instagram": True, "tiktok": True,
        "force_private": True, "max_videos_per_run": 3, "max_retries": 3,
        "github_release_tag": "clip-hosting",
    }
    publishing.update(publishing_overrides)
    return Config(
        database_path=":memory:",
        monitoring=MonitoringConfig(max_retries=1, backoff_base_seconds=0),
        raw={"publishing": publishing, "download": {"work_dir": ""}},
    )


def seed_analyzed(conn, video_id="VID1"):
    source = Source("s", "UCtesta0000000000000000A", enabled=True,
                    rights_cleared=True, rights_note="n")
    sid = db.sync_sources(conn, [source])["s"]
    _, vid = db.record_discovered_video(
        conn, sid, video_id, "A video",
        "https://www.youtube.com/watch?v=%s" % video_id, None,
    )
    conn.execute("UPDATE source_videos SET status = 'ANALYZED' WHERE id = ?", (vid,))
    conn.commit()
    return vid


def install_render_fake(monkeypatch, clip_id, rendered_path="dummy.mp4", status="RENDERED"):
    def fake_render_one_video(conn_, row, config, work_dir, log, ydl_factory=None):
        if status != "RENDERED":
            return render_runner.VideoRenderOutcome(
                row["youtube_video_id"], row["title"], status, "render did not succeed",
            )
        db.record_render(conn_, clip_id, rendered_path, "A Title", "A description", "F1,Shorts")
        return render_runner.VideoRenderOutcome(
            row["youtube_video_id"], row["title"], db.VideoStatus.RENDERED.value,
            clip_id=clip_id,
        )
    monkeypatch.setattr(render_runner, "render_one_video", fake_render_one_video)


def youtube_creds():
    return publish_runner.YouTubeCredentials("id", "secret", "refresh")


def instagram_creds():
    return publish_runner.InstagramCredentials("token", "iguser")


def run(conn, tmp_path, config=None, **kw):
    kw.setdefault("github_token", "GHTOKEN")
    return publish_runner.run_publish(
        conn, config or make_config(), work_dir=tmp_path, log=get_logger("test"), **kw
    )


# --- platform independence ------------------------------------------------


def test_both_platforms_succeeding_publishes_the_video(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)
    monkeypatch.setattr(publish_runner.youtube, "upload",
                        lambda *a, **kw: publish_runner.youtube.UploadResult("YT1", "https://yt/YT1", "private"))
    monkeypatch.setattr(publish_runner.instagram, "publish",
                        lambda *a, **kw: publish_runner.instagram.PublishResult("IG1"))

    result = run(conn, tmp_path, youtube_creds=youtube_creds(), instagram_creds=instagram_creds())

    assert result.published == 1
    outcome = result.outcomes[0]
    assert outcome.status == "PUBLISHED"
    statuses = {p.platform: p.status for p in outcome.platforms}
    assert statuses == {"youtube": "PUBLISHED", "instagram": "PUBLISHED"}

    clip_row = db.get_clip(conn, clip_id)
    assert clip_row["status"] == "PUBLISHED"
    video_row = db.get_video(conn, "VID1")
    assert video_row["status"] == "PUBLISHED"
    assert db.publication_status(conn, clip_id, "youtube") == "PUBLISHED"
    assert db.publication_status(conn, clip_id, "instagram") == "PUBLISHED"


def test_one_platform_failing_still_publishes_the_video(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)
    monkeypatch.setattr(publish_runner.youtube, "upload",
                        lambda *a, **kw: publish_runner.youtube.UploadResult("YT1", "https://yt/YT1", "private"))

    def boom(*a, **kw):
        raise PublishError("instagram is down")
    monkeypatch.setattr(publish_runner.instagram, "publish", boom)

    result = run(conn, tmp_path, youtube_creds=youtube_creds(), instagram_creds=instagram_creds())

    assert result.published == 1
    statuses = {p.platform: p.status for p in result.outcomes[0].platforms}
    assert statuses == {"youtube": "PUBLISHED", "instagram": "FAILED"}
    assert db.publication_status(conn, clip_id, "instagram") == "FAILED"


def test_every_platform_failing_fails_the_video_and_bumps_retry(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)

    def boom(*a, **kw):
        raise PublishError("down")
    monkeypatch.setattr(publish_runner.youtube, "upload", boom)
    monkeypatch.setattr(publish_runner.instagram, "publish", boom)

    result = run(conn, tmp_path, youtube_creds=youtube_creds(), instagram_creds=instagram_creds())

    assert result.published == 0
    assert len(result.failed) == 1
    video_row = db.get_video(conn, "VID1")
    assert video_row["status"] == "FAILED"
    assert video_row["retry_count"] == 1
    # The clip itself genuinely rendered — that fact is not erased by a
    # later publish failure.
    assert db.get_clip(conn, clip_id)["status"] == "RENDERED"


# --- skips: disabled platforms, missing credentials, idempotency ---------


def test_a_disabled_platform_is_skipped_without_being_attempted(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)
    monkeypatch.setattr(publish_runner.youtube, "upload",
                        lambda *a, **kw: publish_runner.youtube.UploadResult("YT1", "u", "private"))
    called = []
    monkeypatch.setattr(publish_runner.instagram, "publish", lambda *a, **kw: called.append(1))

    config = make_config(instagram=False)
    result = run(conn, tmp_path, config=config, youtube_creds=youtube_creds(), instagram_creds=instagram_creds())

    statuses = {p.platform: p.status for p in result.outcomes[0].platforms}
    assert statuses["instagram"] == "SKIPPED"
    assert called == []


def test_missing_credentials_skip_that_platform(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)
    monkeypatch.setattr(publish_runner.youtube, "upload",
                        lambda *a, **kw: publish_runner.youtube.UploadResult("YT1", "u", "private"))

    result = run(conn, tmp_path, youtube_creds=youtube_creds(), instagram_creds=None)

    statuses = {p.platform: p.status for p in result.outcomes[0].platforms}
    assert statuses["instagram"] == "SKIPPED"
    assert "not set" in next(p.detail for p in result.outcomes[0].platforms if p.platform == "instagram")


def test_an_already_published_platform_is_not_attempted_again(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    db.record_publication(conn, clip_id, "youtube", db.PublicationStatus.PUBLISHED, platform_post_id="OLD")
    install_render_fake(monkeypatch, clip_id)

    called = []
    monkeypatch.setattr(publish_runner.youtube, "upload", lambda *a, **kw: called.append(1))
    monkeypatch.setattr(publish_runner.instagram, "publish",
                        lambda *a, **kw: publish_runner.instagram.PublishResult("IG1"))

    result = run(conn, tmp_path, youtube_creds=youtube_creds(), instagram_creds=instagram_creds())

    assert called == []  # youtube.upload was never called again
    statuses = {p.platform: p.status for p in result.outcomes[0].platforms}
    assert statuses["youtube"] == "PUBLISHED"
    assert statuses["instagram"] == "PUBLISHED"


def test_tiktok_is_never_attempted_even_when_enabled_in_config(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)
    monkeypatch.setattr(publish_runner.youtube, "upload",
                        lambda *a, **kw: publish_runner.youtube.UploadResult("YT1", "u", "private"))
    monkeypatch.setattr(publish_runner.instagram, "publish",
                        lambda *a, **kw: publish_runner.instagram.PublishResult("IG1"))

    result = run(conn, tmp_path, youtube_creds=youtube_creds(), instagram_creds=instagram_creds())
    platforms = {p.platform for p in result.outcomes[0].platforms}
    assert "tiktok" not in platforms


# --- render pass-through --------------------------------------------------


def test_a_video_that_does_not_render_passes_through_unchanged(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id, status="FAILED")

    result = run(conn, tmp_path, youtube_creds=youtube_creds())

    assert result.outcomes[0].status == "FAILED"
    assert result.outcomes[0].platforms == []


# --- Phase 9: daily cap and cross-video selection -------------------------


def test_the_best_scored_video_across_all_is_published_first(conn, tmp_path, monkeypatch):
    low_vid = seed_analyzed(conn, "LOW")
    high_vid = seed_analyzed(conn, "HIGH")
    _insert_candidate(conn, low_vid, score=10.0)
    high_clip = _insert_candidate(conn, high_vid, score=99.0)

    install_render_fake(monkeypatch, high_clip)
    monkeypatch.setattr(publish_runner.youtube, "upload",
                        lambda *a, **kw: publish_runner.youtube.UploadResult("YT1", "u", "private"))

    config = make_config(instagram=False, max_videos_per_run=1)
    result = run(conn, tmp_path, config=config, youtube_creds=youtube_creds())

    assert len(result.outcomes) == 1
    assert result.outcomes[0].video_id == "HIGH"


def test_a_second_video_is_blocked_once_the_daily_cap_is_spent(conn, tmp_path, monkeypatch):
    vid1 = seed_analyzed(conn, "VID1")
    vid2 = seed_analyzed(conn, "VID2")
    clip1 = _insert_candidate(conn, vid1, score=99.0)
    _insert_candidate(conn, vid2, score=50.0)

    install_render_fake(monkeypatch, clip1)
    monkeypatch.setattr(publish_runner.youtube, "upload",
                        lambda *a, **kw: publish_runner.youtube.UploadResult("YT1", "u", "private"))

    config = make_config(instagram=False, max_videos_per_run=5, max_per_day=1)
    result = run(conn, tmp_path, config=config, youtube_creds=youtube_creds())

    assert len(result.outcomes) == 1
    assert result.outcomes[0].video_id == "VID1"
    assert db.get_video(conn, "VID2")["status"] == "ANALYZED"  # never attempted


def test_publish_refuses_a_new_clip_once_the_cap_was_already_spent_today(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    other_clip = _insert_candidate(conn, vid, start_ms=0, end_ms=20000)
    # A different clip already published today spends the one slot.
    other_vid = seed_analyzed(conn, "OTHER")
    unrelated_clip = _insert_candidate(conn, other_vid)
    db.record_render(conn, unrelated_clip, "x.mp4", "t", "d", "h")
    db.record_publication(conn, unrelated_clip, "youtube", db.PublicationStatus.PUBLISHED)
    db.mark_clip_published(conn, unrelated_clip)

    called = []
    install_render_fake(monkeypatch, other_clip)
    monkeypatch.setattr(publish_runner.youtube, "upload", lambda *a, **kw: called.append(1))

    config = make_config(instagram=False, max_per_day=1, max_videos_per_run=5)
    result = run(conn, tmp_path, config=config, youtube_creds=youtube_creds())

    assert result.daily_cap_reached is True
    assert called == []
    assert result.outcomes == []


def test_a_clip_already_published_today_on_one_platform_can_still_finish_another(
    conn, tmp_path, monkeypatch
):
    """The exact scenario the cap must not block: a crash (or earlier
    partial run) left one platform PUBLISHED and the other untried. Finishing
    it does not spend a second slot."""
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    db.record_publication(conn, clip_id, "youtube", db.PublicationStatus.PUBLISHED)

    install_render_fake(monkeypatch, clip_id)
    monkeypatch.setattr(publish_runner.instagram, "publish",
                        lambda *a, **kw: publish_runner.instagram.PublishResult("IG1"))

    config = make_config(max_per_day=1)
    result = run(conn, tmp_path, config=config, instagram_creds=instagram_creds())

    assert result.daily_cap_reached is True  # true of the day, reported honestly
    assert len(result.outcomes) == 1          # but the continuation still ran
    assert result.outcomes[0].status == "PUBLISHED"


# --- environment pre-check -------------------------------------------------


def test_no_platform_credentials_renders_nothing_and_consumes_no_clip(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)

    result = run(conn, tmp_path, youtube_creds=None, instagram_creds=None)

    assert result.environment_error is not None
    assert result.outcomes == []
    assert db.get_clip(conn, clip_id)["status"] == "CANDIDATE"
    video_row = db.get_video(conn, "VID1")
    assert video_row["status"] == "ANALYZED"
    assert video_row["retry_count"] == 0


def test_credentials_only_for_a_disabled_platform_still_render_nothing(conn, tmp_path, monkeypatch):
    vid = seed_analyzed(conn)
    clip_id = _insert_candidate(conn, vid)
    install_render_fake(monkeypatch, clip_id)

    result = run(conn, tmp_path, config=make_config(youtube=False),
                 youtube_creds=youtube_creds(), instagram_creds=None)

    assert result.environment_error is not None
    assert db.get_clip(conn, clip_id)["status"] == "CANDIDATE"


def test_run_publish_refuses_when_filters_are_missing(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(publish_runner.video_ffmpeg, "missing_required_filters",
                        lambda: ["ass", "drawtext"])
    seed_analyzed(conn)
    result = run(conn, tmp_path)
    assert result.environment_error is not None
    assert result.outcomes == []
    assert db.get_video(conn, "VID1")["status"] == "ANALYZED"


def _insert_candidate(conn, video_row_id, start_ms=0, end_ms=20000, score=90.0):
    cur = conn.execute(
        "INSERT INTO clips (source_video_id, start_ms, end_ms, score, reason, "
        " hook, status, created_at) VALUES (?, ?, ?, ?, 'r', 'h', 'CANDIDATE', ?)",
        (video_row_id, start_ms, end_ms, score, db.utcnow()),
    )
    conn.commit()
    return cur.lastrowid
