from pathlib import Path

from src.download import workspace


def test_ephemeral_dir_is_created_then_removed():
    with workspace.work_dir() as path:
        assert path.is_dir()
        (path / "video.mp4").write_bytes(b"x" * 10)
        captured = path
    assert not captured.exists()


def test_cleanup_happens_even_when_the_body_raises():
    """A failed download must not leak gigabytes."""
    captured = {}
    try:
        with workspace.work_dir() as path:
            captured["path"] = path
            (path / "half.mp4").write_bytes(b"x" * 10)
            raise RuntimeError("download blew up")
    except RuntimeError:
        pass
    assert not captured["path"].exists()


def test_keep_leaves_the_directory_in_place():
    with workspace.work_dir(keep=True) as path:
        (path / "f").write_bytes(b"x")
        captured = path
    assert captured.exists()
    import shutil
    shutil.rmtree(captured)


def test_a_configured_dir_is_created_and_not_removed(tmp_path):
    target = tmp_path / "work"
    with workspace.work_dir(str(target)) as path:
        assert path == target.resolve()
        (path / "f").write_bytes(b"x")
    assert target.exists()
    assert (target / "f").exists()


def test_dir_size_sums_nested_files(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b").write_bytes(b"x" * 50)
    assert workspace.dir_size_bytes(tmp_path) == 150
