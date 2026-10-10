"""A fake yt-dlp whose info dict matches what the real extractor returned.

Grounded in a live probe on 2026-10-10 (Blender's Big Buck Bunny via the
android_vr client), so these fakes are not invented: `license` is ABSENT
rather than None, `live_status` is 'not_live', and the real response carried
exactly one progressive format at 640x360.
"""

REAL_INFO = {
    "id": "YE7VzlLtp-4",
    "title": "Big Buck Bunny",
    "duration": 597,
    # NOTE: no "license" key at all. This is why the licence decision comes
    # from the Data API and never from here.
    "is_live": False,
    "live_status": "not_live",
    "was_live": False,
    "availability": "public",
    "filesize_approx": 25337037,
    "ext": "mp4",
    "width": 640,
    "height": 360,
    "fps": 24,
    "channel_id": "UCSMOQeBJ2RAnuFungnQOxLg",
    "age_limit": 0,
}


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL as a context manager."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):  # pragma: no cover - overridden
        raise NotImplementedError


def factory(script, calls=None, write_file=None):
    """Build a yt-dlp factory from a per-client script.

    ``script`` maps a player client name to either an info dict (success) or
    an Exception instance (failure). A client absent from the script fails
    with a generic transient error.

    ``write_file`` is called as write_file(dest_dir, info) on a successful
    download, so a test can decide whether a file actually appears.
    """
    calls = calls if calls is not None else []

    def make(opts):
        client = "default"
        extractor_args = opts.get("extractor_args") or {}
        clients = (extractor_args.get("youtube") or {}).get("player_client") or []
        if clients:
            client = clients[0]

        class Scripted(FakeYDL):
            def extract_info(self, url, download=False):
                calls.append({"client": client, "download": download, "opts": opts})
                outcome = script.get(client, RuntimeError("Failed to extract"))
                if isinstance(outcome, Exception):
                    raise outcome
                info = dict(outcome)
                if download and write_file is not None:
                    from pathlib import Path

                    out = opts.get("outtmpl") or ""
                    dest = Path(out).parent if out else Path(".")
                    written = write_file(dest, info)
                    if written is not None:
                        info["requested_downloads"] = [{"filepath": str(written)}]
                return info

        return Scripted(opts)

    make.calls = calls
    return make


def writes_a_file(size_bytes=2048, ext="mp4"):
    """A write_file callback that creates a plausible output file."""

    def write(dest, info):
        from pathlib import Path

        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / ("%s.%s" % (info["id"], ext))
        path.write_bytes(b"\0" * size_bytes)
        return path

    return write


def writes_nothing(dest, info):
    """Simulates yt-dlp reporting success with no file on disk."""
    return None
