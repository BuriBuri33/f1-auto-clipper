# F1 Auto-Clipper

An automated short-form clipping pipeline: watch source channels, find the
strongest moment in a new video, render it vertically with captions, and
publish one clip per day to YouTube Shorts, Instagram Reels and TikTok.

Designed to run entirely on free tiers (target: **₹0/month** recurring) and
entirely in CI, so nothing depends on your computer being on.

> **Status: Phases 1-3 of 9 complete** — source monitoring, new-video
> detection, gated downloading, and transcription. Clip selection, rendering
> and publishing are scaffolded but not implemented. See [TODO.md](TODO.md).
>
> Downloading is **verified working** end to end (12.9 MB through the real
> code path). The earlier failure was a year-stale yt-dlp, not a blocked
> network — a floor of `yt-dlp>=2026.8.19` and a `doctor` check now guard
> against it recurring. Still to confirm: the same from a GitHub runner.
> [docs/LIMITS.md §7](docs/LIMITS.md).

---

## Before you start: two things that will shape your plan

1. **Rights.** The pipeline only processes sources you have marked as
   rights-cleared, and official F1 broadcast footage is not in that category.
   Read [docs/RIGHTS.md](docs/RIGHTS.md) first — it determines whether this
   project is useful to you at all.
2. **Run `python -m src.main doctor` first.** It checks, in order: yt-dlp is
   new enough, FFmpeg is present, your API key works, metadata is readable,
   and media bytes can actually be fetched. An out-of-date yt-dlp fails with
   403s that look exactly like being IP-blocked, so this check saves you
   debugging the wrong thing.
3. **Public posting on YouTube and TikTok is gated behind a free manual
   audit.** Until those audits pass, API uploads are forced private
   (YouTube) and `SELF_ONLY` (TikTok), whatever your code requests. This is
   platform policy, not a bug, and it cannot be coded around. Details and
   figures in [docs/LIMITS.md](docs/LIMITS.md).

## Transcription

Two backends behind one interface (`transcribe(media_path) -> Transcript`):

- **Groq Whisper** (free tier) when `TRANSCRIPTION_API_KEY` is set. The cap
  that binds is **25 MB per upload**, not the daily seconds, so long audio is
  chunked — each chunk encoded directly from the source at a bitrate we pick,
  with overlapping chunks and non-overlapping "authoritative windows" so
  speech is kept by exactly one chunk.
- **Local `faster-whisper`** otherwise, and as a fallback when a Groq attempt
  fails. No key, no quota, no network. A *spent allowance* deliberately does
  **not** fall back: quietly burning an hour of CPU every run would hide the
  problem.

Word-level timestamps are on by default, and segments are rebuilt from them
rather than trusting the backend's grouping. That is not polish — measured
against audio with known timing, segment end times were wrong by over 4
seconds without them, and the backend's grouping changed with the audio
encoding while the word timings stayed correct.
[docs/LIMITS.md §4](docs/LIMITS.md) has the measurements.

## Architecture

```
source channels
      |
      v
upload detector  ──  YouTube Data API (primary, 1 quota unit/check)
      |              Atom RSS feed    (fallback, no credentials)
      v
  SQLite state  ──  committed to the repo; CI runners are ephemeral
      |
      v
  ingest: downloader (yt-dlp) -> audio extract -> transcribe
          (one job: media exists only inside a single run)
      |
      v
  clip candidates (LLM or heuristic)  ->  F1-weighted scoring
      |
      v
  FFmpeg render: 9:16, 1080x1920, burned-in captions, watermark
      |
      v
  quality gate  ->  daily queue (max 1/day)
      |
      +--> YouTube Shorts     +--> Instagram Reels     +--> TikTok
```

Each stage is a separate module behind a narrow interface, so any of them can
be swapped — which is the point: every external service in the chain is a
free tier that may change.

## Quick start

**Requires Python 3.10+ (3.12 recommended, and what CI uses).** Current
yt-dlp refuses to install on 3.9, so this is a hard floor, not a preference.
Also requires **FFmpeg** on PATH.

```bash
brew install python@3.12 ffmpeg       # macOS
# sudo apt-get install -y python3.12 ffmpeg   # Ubuntu
```

```bash
git clone <your-repo-url> && cd f1-auto-clipper
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest tests -q          # 216 tests, no network needed
python -m src.main init-db
python -m src.main doctor          # can this machine download?
python -m src.main monitor --dry-run
```

FFmpeg is not optional: YouTube serves exactly one single-file format and it
is 360p, so anything usable for a 1080x1920 render has to be muxed from
separate video and audio streams.

If your package index is stale and offers an old yt-dlp, install from source
(`doctor` tells you if this applies):

```bash
pip install --upgrade "yt-dlp @ git+https://github.com/yt-dlp/yt-dlp.git"
```

### Add a source

Find the channel id (it is the `UC...` form, not the `@handle`):

```bash
python -m src.main resolve-channel "https://www.youtube.com/@somechannel"
```

Then add it to `config/sources.yaml`:

```yaml
sources:
  - name: my_own_channel
    channel_id: "UCxxxxxxxxxxxxxxxxxxxxxx"
    enabled: true
    rights_cleared: true
    rights_note: "My own uploads."
```

`rights_cleared: true` requires a non-empty `rights_note` — the loader
rejects the claim otherwise. A source with `rights_cleared: false` is still
monitored, but everything it produces is recorded as `SKIPPED` and never
downloaded.

### Run a real check

```bash
export YOUTUBE_API_KEY=...        # optional but strongly recommended
python -m src.main monitor
python -m src.main status
```

Without `YOUTUBE_API_KEY` the monitor falls back to YouTube's Atom feed,
which needs no credentials but returns intermittent 404s from datacenter IPs
— including GitHub Actions runners. Fine for a local try; not dependable in
CI. See [docs/LIMITS.md §6](docs/LIMITS.md).

**Phase 2 requires the key outright.** The per-video licence check runs
through the Data API, and without it nothing can be verified — so nothing is
downloaded. That is deliberate: an unverifiable video is deferred, never
fetched on the assumption it is fine.

### The rights gate, in two layers

`rights_cleared` on a *source* is necessary but not sufficient, because
licence is a property of each **video**. A Creative Commons channel can still
contain all-rights-reserved uploads. So each download candidate is also
checked with `videos.list?part=status` (1 quota unit per batch of 50) and
skipped unless `status.license` is `creativeCommon`.

A CC marking means the uploader applied that licence — not that they had the
right to. The check is a filter, not a clearance;
[docs/RIGHTS.md](docs/RIGHTS.md) is explicit about where your judgement is
still required.

## Dependencies

Three sets, deliberately separate so a downloader problem cannot stop
detection:

| File | Contains | Used by |
|---|---|---|
| `requirements.txt` | PyYAML, requests | `monitor` workflow, core commands |
| `requirements-download.txt` | the above + `yt-dlp>=2026.8.19` | — |
| `requirements-transcribe.txt` | the above + `faster-whisper` | `ingest` workflow |
| `requirements-dev.txt` | the above + pytest | local dev, `tests` workflow |

Monitoring genuinely runs with yt-dlp absent — verified, not assumed. That
matters because yt-dlp is the one dependency that breaks regularly.

## Commands

| Command | What it does |
|---|---|
| `init-db` | Create the database and sync `sources.yaml` into it |
| `monitor` | Check every enabled source for new uploads |
| `monitor --dry-run` | List what would be checked; make no requests |
| `download` | Download videos that pass the pre-download gate |
| `download --dry-run` | List download candidates; fetch nothing |
| `ingest` | Download **and** transcribe in one pass — what CI runs |
| `prune-transcripts` | Delete transcripts for PUBLISHED/SKIPPED videos |
| `check VIDEO...` | Run the pre-download gate on specific videos; fetch nothing |
| `doctor` | Check whether this environment can download at all |
| `status` | Print a pipeline status report (also to the Actions summary) |
| `sources` | List known sources with their rights and last-check state |
| `source-enable` / `source-disable` | Manual override for one source |
| `retry [VIDEO_ID] [--reset-count]` | Requeue failed videos |
| `unskip VIDEO_ID` | Return one skipped video to the queue (the gate re-runs) |
| `resolve-channel URL` | Find the `UC...` id behind a channel URL or handle |

## Credentials

**Phase 1 needs none.** `YOUTUBE_API_KEY` is optional but makes detection
reliable; everything else is required only once its phase lands.

Get a key: [Google Cloud Console](https://console.cloud.google.com/) → create
a project → enable **YouTube Data API v3** → Credentials → *Create
credentials* → *API key*. Restrict it to the YouTube Data API. This is an API
key, not OAuth — detection only reads public data.

Copy `.env.example` to `.env` for local runs, then fill in the values there.
`.env` is loaded automatically by every command, and **a real environment
variable always wins over the file** — so a local `.env` can never shadow a
GitHub Actions secret in CI.

⚠️ **Put real values in `.env`, never in `.env.example`.** The template is
committed; `.env` is gitignored. A test
(`tests/test_env_template.py`) fails if a credential-shaped variable in the
template has a value, which is the guard rail — but the habit matters more
than the guard.

### As GitHub Actions secrets

Repository → *Settings* → *Secrets and variables* → *Actions* → *New
repository secret*. Use the same names as `.env.example`:

| Secret | Needed from |
|---|---|
| `YOUTUBE_API_KEY` | Phase 1 (optional), **Phase 2 (required)** |
| `TRANSCRIPTION_API_KEY` | Phase 3 (optional — local fallback needs no key) |
| `LLM_API_KEY` | Phase 4 |
| `BRAND_NAME` | Phase 5 |
| `YOUTUBE_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` | Phase 6 |
| `META_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID` | Phase 7 |
| `TIKTOK_CLIENT_KEY` / `_CLIENT_SECRET` / `_ACCESS_TOKEN` | Phase 8 |

The `monitor` workflow is given only `YOUTUBE_API_KEY`, so publishing
credentials are never exposed to it.

## How the automation runs

`.github/workflows/monitor.yml` runs every 3 hours and on manual dispatch. It
checks each source, records new videos, writes a status summary to the run
page, and commits the updated state database back to the repo — runners are
ephemeral, so the committed database *is* the pipeline's memory.

`.github/workflows/ingest.yml` runs every 6 hours, upgrades yt-dlp, asserts
FFmpeg, runs `doctor` as a diagnostic step, then downloads **and transcribes**
what passed the gate. It is separate from `monitor` on purpose: monitoring is
cheap and reliable, ingesting is neither, and an ingest problem must not stop
new uploads being detected.

**Why download and transcription are one job.** The media file is hundreds of
megabytes: too large to commit, and the runner is destroyed when the job ends.
So it exists only inside that run, and anything needing it must happen there —
transcribing in a later workflow would mean downloading the video twice. What
survives is the transcript (text, committed) and the database row.

`.github/workflows/tests.yml` runs the test suite on push and PR.

`daily_clip.yml` is intentionally absent until there is something for it to
do (Phase 5/6). A scheduled workflow that cannot produce a clip is just a
failing job on a timer.

**Public vs private repository matters.** Standard runners are free and
unmetered on public repos (4 vCPU, 16 GB); a private repo gets 2,000
minutes/month on a 2-vCPU runner. Public also gives you the free public HTTPS
hosting that Instagram's API requires for `video_url`. Secrets stay encrypted
either way. Trade-off: on a public repo your source list and clip history are
world-readable. [docs/LIMITS.md §1](docs/LIMITS.md).

## State and idempotency

Everything durable lives in `data/state.db` (SQLite, committed). Duplicate
prevention is enforced by the schema rather than by application checks, so a
re-run or a mid-flight timeout cannot produce a double post:

- `UNIQUE(source_id, youtube_video_id)` — a video is discovered once
- `UNIQUE(source_video_id, start_ms, end_ms)` — a timestamp range is one clip
- `UNIQUE(clip_id, platform)` — a clip is published once per platform

Re-discovering a video never resets its status, so work in progress and
already-published videos are left alone.

Media files are never committed. They are downloaded into a temp directory
that is removed when the command exits, including on failure. What travels
between runs is the database and the transcripts in `data/transcripts/`.

Transcripts are committed because later phases genuinely need them and the
runner that produced them is gone. They are a few hundred kB each, so
`prune-transcripts` releases them once a video reaches PUBLISHED or SKIPPED.

The schema is versioned and migrated in place, so pulling Phase 2 onto a
Phase 1 database adds the new columns without losing rows.

## Documentation

- [CLAUDE.md](CLAUDE.md) — conventions and constraints for working in this repo
- [TODO.md](TODO.md) — phase checklist
- [docs/LIMITS.md](docs/LIMITS.md) — verified free-tier figures and platform gates
- [docs/RIGHTS.md](docs/RIGHTS.md) — rights assumptions and what this project will not implement
- [docs/SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md) — section-by-section audit against the spec, with every deliberate deviation and its reasoning

## Licence

No licence chosen yet. Add one before making the repository public if you
intend others to reuse the code.
