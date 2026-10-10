# TODO

Phase checklist. Section-by-section conformance against the spec, including
every deliberate deviation and why, lives in
[docs/SPEC-CONFORMANCE.md](docs/SPEC-CONFORMANCE.md).

## Blocking, not code

Three things gate "done" and none of them is a coding task. Two are queues
you do not control, so start them early:

- [x] ~~Measure whether downloading works at all.~~ **Done — it was a stale
      yt-dlp, not an IP block.** 2025.10.14 failed on all 9 clients;
      2026.08.19 fetched 12.9 MB through the real code path. Guarded by a
      version floor, a `doctor` check and `--upgrade` in the workflow.
- [ ] **Confirm the same from a GitHub runner.** Push, then read the `doctor`
      step in the `download` workflow run. Very likely fine now that the
      cause is known to be a dependency version rather than IP reputation —
      but likely is not measured. docs/LIMITS.md §7
- [ ] **Start the YouTube compliance audit.** Until it passes, API uploads are
      locked private whatever `privacyStatus` is sent. Free, manual, no
      published turnaround. docs/LIMITS.md §2
- [ ] **Start the TikTok audit.** Until it passes, every post is `SELF_ONLY`.
      Free, manual, reported 2-4 weeks. docs/LIMITS.md §3

## Phase 1 — Monitoring ✅

- [x] Config system: `config.yaml` + `sources.yaml`, `${VAR}` substitution,
      strict validation (unknown keys rejected, channel-id shape checked)
- [x] Rights gate: `rights_cleared` + mandatory `rights_note`, enforced on
      every detector path
- [x] SQLite schema: sources, source_videos, clips, publications, api_usage,
      with the three uniqueness constraints that make the pipeline idempotent
- [x] Primary detector: YouTube Data API `playlistItems.list` (1 quota unit)
- [x] Fallback detector: Atom RSS with conditional GET (ETag / 304)
- [x] Backend selection `auto | api | rss`, with fallback on API failure but
      *not* on a spent quota
- [x] New-video detection with a configurable discovery window
- [x] Structured logging with job/video/clip/platform/retry context
- [x] CLI: init-db, monitor, status, sources, source-enable/disable,
      resolve-channel
- [x] 97 tests, no network required
- [x] `monitor.yml` (every 3h, commits state) and `tests.yml`
- [x] README, CLAUDE.md, docs/LIMITS.md, docs/RIGHTS.md

**Not done in Phase 1, deliberately:** `daily_clip.yml` (nothing to run yet),
`src/utils/config.py` typed sections for clip/video/publishing (read via
`config.section()` until their phase lands).

## Phase 2 — Download ✅ (code complete; one check unverifiable here)

- [x] yt-dlp wrapper with a player-client fallback chain, preferring
      H.264 + AAC in MP4 within a configurable height cap
- [x] Enforce `max_source_duration_minutes` / `max_download_size_mb` before
      any bytes are fetched (duration from the Data API, size from the probe)
- [x] Second rights check at the download boundary, not just at discovery
- [x] **Per-video licence check** via `videos.list?part=status` — 1 quota unit
      per batch of 50, skips anything not `creativeCommon`, records the
      licence as the audit trail
- [x] Three-way gate verdict: ALLOW / SKIP (terminal) / DEFER (retry later),
      so "cannot verify" never reads as either eligible or ineligible
- [x] Temp-directory lifecycle with cleanup guaranteed through `finally`
- [x] Status transitions DISCOVERED → DOWNLOADED / SKIPPED / FAILED with
      retry counting and a retry ceiling
- [x] Livestreams deferred (Data API `liveBroadcastContent` + yt-dlp
      `live_status`); too-short videos skipped
- [x] Schema v2 migration, idempotent and tested against a hand-built v1 db
- [x] `doctor` command — measures whether this environment can download
- [x] `download.yml` workflow, with `pip install --upgrade yt-dlp`
- [x] 216 tests, fakes grounded in a live probe of the real extractor output

**✅ Download verified working.** The earlier failure was a year-stale
yt-dlp, not a blocked network — see docs/LIMITS.md §7 for the measurement.
Now guarded by `yt-dlp>=2026.8.19`, a `doctor` version check that runs before
everything else, and `--upgrade yt-dlp` on every workflow run.

Also done while firming up for Phase 3:
- [x] `.env` is actually loaded now (it was documented but never read), with
      real environment variables taking precedence so a local file can never
      shadow a CI secret
- [x] `check VIDEO...` — run the gate on specific videos, no download,
      1 quota unit for up to 50
- [x] `doctor` validates the API key with a real call instead of checking
      that it is merely present
- [x] Dependencies split so monitoring cannot break because of yt-dlp
- [x] Full suite verified on Python 3.12 (CI's version); Python 3.10+ is now
      a hard floor because current yt-dlp requires it

## Spec gaps closed in the 2026-10-10 conformance audit

- [x] §24 `YOUTUBE_API_KEY` was missing from `.env.example` despite Phase 2
      requiring it — a fresh clone would have deferred everything with no
      indication why. Added with full Cloud Console setup steps.
- [x] §31 status output aligned to the spec's illustrated shape (New videos /
      Processing / Candidates / Today's clip / per-platform lines)
- [x] §29 manual override: `retry [VIDEO_ID] [--reset-count]` and
      `unskip VIDEO_ID`
- [x] §5/§7/§30 deviations documented rather than silently carried
      (`start_ms`/`end_ms`, two config files, absent `daily_clip.yml`,
      added modules)

Still open against the spec, both deferred by phase rather than overlooked:

- [ ] §25 operator notification on free-tier exhaustion (currently relies on
      GitHub's failed-workflow email) — Phase 9
- [ ] §5 `daily_clip.yml` — Phase 5/6, when there is something for it to run

## Phase 3 — Transcription ✅

- [x] `transcribe(media_path) -> Transcript` interface; the spec's
      `{"segments": [{start, end, text}]}` shape is the whole contract
- [x] Groq backend: `verbose_json`, word granularity, 25 MB-aware chunking,
      daily-allowance exhaustion distinguished from a per-minute rate limit
- [x] `faster-whisper` local fallback — no key, no quota, no network
- [x] Audio extraction at 16 kHz mono, chunks encoded direct from source at a
      bitrate we control (one lossy generation, predictable size)
- [x] Overlapping chunks with authoritative windows, so speech is kept by
      exactly one chunk — no duplicates, no gaps
- [x] Word timestamps on by default (without them segment ends are wrong by
      seconds) and segments rebuilt from words rather than trusting the
      backend's grouping
- [x] Fallback policy: a Groq failure falls back to local; a SPENT ALLOWANCE
      does not, because silently burning an hour of CPU hides the problem
- [x] `ingest` command — download and transcribe in one pass, because the
      media only exists inside a single workflow run
- [x] `prune-transcripts` — transcripts are committed, so they need a
      retention story; PUBLISHED/SKIPPED videos release theirs
- [x] Schema v3 transcript columns; `ingest.yml` workflow
- [x] **Real-audio integration tests** against synthesised speech with known
      timing: worst start error 0.38s, worst end error 0.19s, across three
      forced chunk boundaries with zero duplicated speech

Tested per spec section 27: timestamp accuracy ✅ (measured against ground
truth), chunk boundaries ✅, long sources ✅ (chunk planning covers 180
minutes), silence/no-speech ✅, **accents ✅ and team radio ✅** (five English
accents speaking F1 radio idiom with driver names: Groq turbo 5.5% WER,
local `base` 12.7%). **Overlapping speech still untested** — synthesised
audio cannot produce genuine crosstalk, so it belongs with the first real
source.

- [x] `vocabulary_prompt` for driver names (5.5% -> 3.6% WER), with the
      labelled-glossary shape stripped because it gets echoed into the
      transcript

## Phase 4 — Clip selection

- [ ] Prompt + strict JSON schema + validation (reject, never coerce)
- [ ] F1-weighted scoring (hook/emotion/context/payoff/shareability)
- [ ] Heuristic scorer as the no-LLM fallback
- [ ] Sentence-boundary snapping; reject clips starting or ending mid-sentence
- [ ] Deduplication: timestamp overlap + transcript similarity
- [ ] Do not publish anything yet

## Phase 5 — Rendering

- [ ] Cut, 9:16 reframe (configurable strategy, robust fallback over bad crops)
- [ ] ASS captions, burned in, mobile-legible, inside safe margins
- [ ] Configurable watermark; skip it if it would block key footage
- [ ] Metadata: title, description, hashtags — no keyword stuffing, no
      implying official F1 content
- [ ] Quality gate (spec §28) — all checks must pass before publish
- [ ] Inspect sample MP4s by hand before wiring any publisher

## Phase 6 — YouTube

- [ ] OAuth refresh-token flow, resumable upload, `privacyStatus: private`
- [ ] Idempotent retry: check for an existing upload before re-posting
- [ ] Start the compliance audit — public Shorts are blocked until it passes

## Phase 7 — Instagram

- [ ] Publish the rendered file to a public HTTPS URL for `video_url`
- [ ] Container create → poll status → publish, with the 90s cap enforced
      at the quality gate

## Phase 8 — TikTok

- [ ] OAuth + Content Posting API, expecting `SELF_ONLY` pre-audit
- [ ] Start the audit

## Phase 9 — Full automation

- [ ] `daily_clip.yml`, max 1 published clip/day enforced in the database
- [ ] `retry` command for failed jobs; manual approve/skip/override of
      candidates and timestamps
- [ ] Status summary in the Actions run page for the whole pipeline
