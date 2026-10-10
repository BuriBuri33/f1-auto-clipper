# F1 Auto-Clipper

An automated short-form clipping pipeline: watch source channels, find the
strongest moment in a new video, render it vertically with captions, and
publish one clip per day to YouTube Shorts, Instagram Reels and TikTok.

Designed to run entirely on free tiers (target: **₹0/month** recurring) and
entirely in CI, so nothing depends on your computer being on.

> **Status: Phases 1-7 of 9 complete** (TikTok/Phase 8 deliberately deferred)
> — source monitoring, new-video detection, gated downloading, transcription,
> clip candidate selection, rendering, and publishing to YouTube Shorts and
> Instagram Reels. **YouTube and Instagram publishing are implemented and
> unit-tested against each platform's documented protocol, but NOT yet
> verified against a live account** — no OAuth client or Instagram
> Professional account was available to test with. Run `python -m src.main
> doctor` once you have credentials, before trusting this the way every
> other phase's measurements have been earned. See [docs/LIMITS.md
> §9](docs/LIMITS.md). See [TODO.md](TODO.md).
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
   new enough, FFmpeg is present (and has the caption/watermark filters
   rendering needs), every API key/credential you've configured actually
   works (YouTube, transcription, clip-selection LLM, YouTube publishing,
   Instagram), and media bytes can actually be fetched. An out-of-date
   yt-dlp fails with 403s that look exactly like being IP-blocked, so this
   check saves you debugging the wrong thing.
3. **Public posting on YouTube and TikTok is gated behind a free manual
   audit.** Until those audits pass, API uploads are forced private
   (YouTube) and `SELF_ONLY` (TikTok), whatever your code requests. This is
   platform policy, not a bug, and it cannot be coded around. Details and
   figures in [docs/LIMITS.md](docs/LIMITS.md).

## Publishing (Phase 6/7)

**Not yet verified against a live account** — see the status banner above
and [docs/LIMITS.md §9](docs/LIMITS.md) before relying on this.

Render and publish are one command (`publish`), for the same reason download
and transcription are one (`ingest`): the rendered file only exists inside
the run that produced it, so anything needing it — including uploading it —
has to happen there. A publish failure cannot be retried on its own in a
later run; there is no file left to retry with, so a retry re-renders and
re-publishes from scratch. What IS idempotent: a platform already recorded
PUBLISHED for a clip is checked and skipped before any upload is attempted,
so a clip that reached YouTube but failed on Instagram will, on retry, skip
straight past the already-done YouTube upload.

Each platform is independent — a clip can succeed on one and fail on the
other in the same run, and a clip is marked PUBLISHED as soon as ANY enabled
platform succeeds. Credentials are genuinely optional per platform: missing
ones mean that platform is SKIPPED, not failed, so `publish` runs sensibly
before either platform's setup is done.

- **YouTube**: OAuth refresh-token flow + resumable upload, raw REST rather
  than the `google-api-python-client`/`google-auth` SDKs (same minimal-deps
  reasoning as Phase 4's Gemini client — the whole protocol is three
  well-documented HTTP calls). Always uploads `privacyStatus: private`
  unless `publishing.force_private: false` — and even then, uploads from an
  un-audited API project are forced private by YouTube regardless of what is
  sent. See "Before you start" above and [docs/LIMITS.md §2](docs/LIMITS.md).
- **Instagram**: the Graph API does not accept a direct file upload — it
  fetches the video from a public `video_url` you give it. So publishing a
  clip means hosting it a SECOND time first: a temporary GitHub Release
  asset (one release, reused across every clip, deleted again once
  Instagram's container finishes processing), using `GITHUB_TOKEN` — minted
  automatically in Actions with the same `contents: write` permission the
  workflow already has, no new secret needed there.

Does not select "today's clip" across videos or enforce the spec's 1/day
cap (both remain Phase 9) — `publish` pushes the best candidate for every
video it considers, bounded by `publishing.max_videos_per_run`.
`.github/workflows/publish.yml` runs once a day as a blunt stand-in for that
cap until Phase 9's real selection logic exists.

## Rendering (Phase 5)

Re-downloads the source video — media is never persisted between workflow
runs (`ingest` downloads and transcribes in one pass for exactly this
reason), so by the time a video is ready to render its file is already gone.
A partial, time-ranged re-fetch was tried first and measured to 403 on every
player client that completes a full download; a full re-download, reusing
the already-proven Phase 2 code, is what actually works. Costs time, not
money. [docs/LIMITS.md §8b](docs/LIMITS.md).

For each video, candidate clips are tried best-score-first: cut, reframed to
9:16, captioned, branded, encoded, then checked against a quality gate
(resolution, codecs, frame rate, duration, stream counts — spec section 28).
A clip that fails the gate is rejected and the next candidate is tried —
spec section 12's "backup candidates", put to use here rather than just
stored.

- **9:16 reframing**: `center_crop` (default) around the middle, falling back
  automatically to `blur_pad` (a blurred, scaled copy filling the frame)
  only when the source's own aspect ratio makes a crop geometrically
  impossible — spec section 15's "robust fallback over bad crops".
- **Captions**: burned in via FFmpeg's `ass` filter, re-chunked from the same
  word-timing primitive Phase 3/4 already use, inside configurable safe
  margins clear of what every Shorts/Reels/TikTok player draws over the
  frame.
- **Watermark**: `drawtext`, restricted to a single corner by construction
  and pushed clear of the caption safe-zone rather than ever overlapping it.
- **Metadata**: title/description/hashtags, a capped hashtag count (no
  keyword stuffing by construction) and a fixed non-affiliation disclaimer
  on every description.

**A present `ffmpeg` binary does not mean any of this works.** Burned
captions need libass, the watermark needs libfreetype, and a plain Homebrew
`ffmpeg` install has neither — `doctor` and `render` both check the actual
filter list, not just that `ffmpeg` is on PATH. [docs/LIMITS.md
§8a](docs/LIMITS.md).

Does not publish anything, and the rendered file is not committed (media
never reaches git) — it exists only inside the workflow run that produced
it until a publisher runs in the same pass (Phase 6+).

## Clip selection (Phase 4)

Candidate clip windows are built on transcript SEGMENT boundaries — never a
slice through one — which directly satisfies "avoid clips that begin or end
mid-sentence" (spec section 10) as a side effect of Phase 3's decision to
rebuild segments on sentence/pause boundaries rather than trust a backend's
own grouping.

Each candidate is scored on five axes (spec section 11: hook, emotional,
context, payoff, shareability), combined by a configurable weighted formula:

- **Gemini** (free tier, `gemini-flash-latest`) when `LLM_API_KEY` is set.
  Output is validated strictly — a malformed field, an out-of-range score,
  or a timestamp the model invented rather than one it was given is
  **rejected, never coerced**.
- **A no-LLM heuristic** otherwise, and as a fallback when a Gemini call
  fails for a reason that is not a spent quota (a spent quota defers the
  work instead — never silently degrades).

Deduplication happens at three levels: the database's own uniqueness
constraint (an exact repeat), in-video overlap filtering (near-identical
windows around the same moment), and cross-video text similarity against
clips already queued or published (so the same moment described slightly
differently in a recap video is caught too).

Does not publish anything, and does not enforce "one clip per day" across
videos — both are explicitly Phase 9's job.
[docs/LIMITS.md §5](docs/LIMITS.md) has the measurements behind the model
choice, the thinking-budget setting, and the scoring rubric.

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
rather than trusting the backend's grouping. That is not polish. Measured
against audio with known timing:

| Backend | worst start error | worst end error |
|---|---|---|
| Groq, word timings unread | 2.00s | 3.32s |
| Groq, word timings attached | **0.08s** | **0.48s** |
| Local `faster-whisper` (`tiny`) | 0.38s | 0.19s |

Both backends return coarse, silence-stretched segments and accurate words;
the accuracy comes entirely from using the words.
[docs/LIMITS.md §4](docs/LIMITS.md) has the details.

Check your setup with `python -m src.main doctor`, which validates the
transcription key with a real call rather than just checking it is present.

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

python -m pytest tests -q          # 665 tests, no network needed
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
| `analyze` | Score transcribed videos into ranked, deduplicated clip candidates |
| `analyze --dry-run` | List videos pending analysis; score nothing |
| `render` | Render the best candidate clip per video: cut, reframe, caption, brand |
| `render --dry-run` | List videos pending render; fetch and encode nothing |
| `render --keep [--work-dir PATH]` | Keep the working directory so a rendered MP4 can be inspected by hand |
| `publish` | Render **and upload** the best candidate clip per video — what CI runs |
| `publish --dry-run` | List videos pending publish; fetch, render and upload nothing |
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
| `LLM_API_KEY` | Phase 4 (optional — heuristic fallback needs no key) |
| `BRAND_NAME` | Phase 5 (optional — unset renders the literal placeholder "YOUR_CHANNEL_NAME"; set `branding.enabled: false` to skip the watermark instead) |
| `YOUTUBE_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` | Phase 6 |
| `META_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID` | Phase 7 |
| `TIKTOK_CLIENT_KEY` / `_CLIENT_SECRET` / `_ACCESS_TOKEN` | Phase 8 — not implemented yet, nothing reads these |

The `monitor` workflow is given only `YOUTUBE_API_KEY`, so publishing
credentials are never exposed to it.

**Instagram publishing also needs `GITHUB_TOKEN` and `GITHUB_REPOSITORY`** —
not secrets you set: both are provided automatically inside GitHub Actions
(`GITHUB_TOKEN` from the workflow's own `permissions: contents: write`,
already granted for committing `data/`). Nothing to add in *Settings >
Secrets*. Locally, exercising Instagram publishing for real needs a personal
access token with the same scope exported as `GITHUB_TOKEN` by hand — see
`src/publishing/hosting.py`.

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

`.github/workflows/analyze.yml` runs every 4 hours and scores transcribed
videos into clip candidates. It is separate from `ingest` too, and able to
be: this stage reads only the committed transcript, never the media that
produced it — which is long gone by the time this ever runs.

**Why download and transcription are one job.** The media file is hundreds of
megabytes: too large to commit, and the runner is destroyed when the job ends.
So it exists only inside that run, and anything needing it must happen there —
transcribing in a later workflow would mean downloading the video twice. What
survives is the transcript (text, committed) and the database row.

`.github/workflows/publish.yml` runs once a day and renders **and uploads**
the best candidate clip for each ANALYZED video — YouTube Shorts and
Instagram Reels; TikTok is skipped unconditionally (Phase 8, not built).
Render and publish are one job for the same reason download and
transcription are: the rendered file only exists inside this run. A
platform with no credentials configured is skipped, not failed, so this
workflow is safe to enable before either OAuth/app setup is done. Once a day
rather than every few hours like the earlier stages — there is no Phase 9
yet to pick the single best clip across every video and enforce "one per
day" for real (spec section 12), so a daily cron is a blunt stand-in for
that cap, not an implementation of it. See
[docs/SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md) section 5.

`.github/workflows/render.yml` is manual-dispatch only: render a clip
without uploading it anywhere, to inspect by hand (`render --keep`) without
touching any publishing credentials.

`.github/workflows/tests.yml` runs the test suite on push and PR.

`daily_clip.yml`, the spec's own name for this stage, is intentionally
absent — `publish.yml` is what actually runs, under a different name and
without the real 1/day selection logic. See the note above.

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
between runs is the database and the transcripts in `data/transcripts/`. A
rendered clip published to Instagram is hosted PUBLICLY but TEMPORARILY (a
GitHub Release asset, not a git commit) only long enough for Instagram's
servers to fetch it, then deleted — see "Publishing" above.

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
