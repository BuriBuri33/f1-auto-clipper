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
- [ ] **Mint real YouTube OAuth / Instagram Graph API credentials and run
      `publish` for real.** Phase 6/7 are implemented and unit-tested against
      each platform's documented protocol, but neither has been exercised
      against a live account — a real gap against this project's own
      "verify, do not recall" rule. `python -m src.main doctor` is the first
      check once credentials exist. docs/LIMITS.md §9

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

Still open against the spec, deferred by phase rather than overlooked:

- [ ] §25 operator notification on free-tier exhaustion (currently relies on
      GitHub's failed-workflow email) — not scheduled against a phase
- [x] §5 `daily_clip.yml` — done in Phase 9 (`publish.yml` renamed, now with
      real database-enforced selection behind it)

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

## Phase 4 — Clip selection ✅

- [x] `generate_windows`/`deduplicate_windows` — candidate spans built on
      sentence/pause-bounded transcript segments, never a mid-sentence slice
- [x] Gemini backend: structured `responseSchema` output, strict validation
      (reject a malformed/out-of-range/invented-timestamp entry, never
      coerce it), `thinkingBudget: 0`, a rubric with explicit 0/50/100
      anchors on every axis (an unanchored "score 0-100" produced wild
      outliers — 9 vs 95 on the same clip)
- [x] Heuristic no-LLM fallback scored on the same five F1-weighted axes,
      using several independent signals so nothing is selected solely for
      one keyword hit (spec section 10)
- [x] `clip_scorer.final_score` — the weighted formula from spec section 11,
      with configurable weights, applied identically to both backends
- [x] Fallback policy matching Phase 3's: an LLM failure falls back to
      heuristic; a SPENT QUOTA does not, and defers instead
- [x] Deduplication: schema UNIQUE constraint (exact repeat), in-video
      overlap filtering (near-identical windows), AND cross-video text
      similarity against already QUEUED/PUBLISHED clips (spec section 13)
- [x] `analyze` command + `analyze.yml` workflow — its own job, because this
      stage reads only the committed transcript, never ephemeral media
- [x] Schema v4: five sub-scores + scoring method stored per clip, so the
      formula's inputs are auditable after the fact (spec section 29)
- [x] **Verified end-to-end against the live Gemini API and a real account**:
      five real candidate windows scored, validated, ranked, overlap-filtered
      to 2, and stored — with the model correctly ranking the crash/safety-car
      segment highest, matching the spec's own F1 priority order
- [x] 94 new tests; one real bug (post-dedup empty result read as ANALYZED
      instead of SKIPPED) caught by a test, not by inspection

Does NOT publish anything (per spec: "Do not publish yet") and does NOT
enforce "1 clip/day" across videos — that selection-across-all-videos step
is explicitly Phase 9's job; `db.best_candidates_overall` is the
infrastructure Phase 9 will query.

## Phase 5 — Rendering ✅

- [x] Cut, 9:16 reframe: `center_crop` (default) with automatic fallback to
      `blur_pad` when the source's own aspect ratio makes a crop geometrically
      impossible — the "robust fallback over bad crops" spec section 15 asks
      for, triggered by an actual impossibility rather than a style judgement
- [x] ASS captions, burned in via FFmpeg's `ass` filter, re-chunked from word
      timings with `transcription.resegment_from_words` (tighter limits than
      Phase 3/4's reuse of the same primitive), inside configurable safe
      margins clear of platform UI
- [x] Configurable watermark (`drawtext` + `textfile=`, not inline text, so
      operator-supplied `BRAND_NAME` never needs escaping) — restricted to a
      single corner by construction, and a bottom corner is pushed clear of
      the caption safe-zone rather than ever overlapping it
- [x] Metadata: title/description/hashtags, capped hashtag count (no keyword
      stuffing by construction) and a fixed non-affiliation disclaimer on
      every description (no implying official F1 content)
- [x] Quality gate (spec §28): resolution, codecs, frame rate, duration and
      stream counts all checked against the FINISHED file via `ffprobe`, not
      the command line that produced it; every failure collected, not just
      the first
- [x] `render` command + `render.yml` workflow — re-downloads the source
      (media is not persisted between runs; a partial time-ranged fetch was
      tried and measured to 403, see docs/LIMITS.md §8b) and tries each
      CANDIDATE clip best-score-first, falling to the next on a rejected gate
      or a failed encode (spec section 12's "backup candidates", now used)
- [x] Schema v5: `clips.status_detail` / `clips.rendered_at`, so a rejected
      candidate's reason is on the record the same way a failed video's is
- [x] **Verified end-to-end against a real FFmpeg encode** (not only unit
      tests with fakes): both reframe strategies, burned captions and the
      watermark composited correctly on a synthetic source, inspected by eye;
      caught one real bug a faked-`render_clip` unit test could not (missing
      `.mp4` extension on the output path) — see docs/LIMITS.md §8c
- [x] Inspect sample MP4s by hand — done locally (`render --keep` writes to
      the working directory); not yet done against a REAL F1 source, which
      needs a GitHub runner confirmed to download (see "Blocking, not code")

**Known gap, deliberately not closed here:** a plain `ffmpeg` build (e.g.
Homebrew's default formula) lacks the `ass`/`drawtext` filters this phase
needs — confirmed locally, NOT yet confirmed on a GitHub Actions runner. See
docs/LIMITS.md §8a. `doctor` and `render` both check for this explicitly and
fail clearly rather than produce a captionless/unbranded clip silently.

Does NOT publish anything, and does NOT select "the one clip for today"
across videos (spec section 12's selection step) — both remain Phase 9. A
RENDERED clip is not committed anywhere; it exists only inside the workflow
run that produced it until a publisher runs in the same pass.

## Phase 6 — YouTube ✅ (code complete; unverified against a live account)

- [x] OAuth refresh-token flow (raw REST, not the google-api-python-client/
      google-auth SDKs the original dependency plan named — same minimal-deps
      reasoning as Phase 4's Gemini client), resumable upload with a real
      query-and-resume recovery path, `privacyStatus: private` by default
      (`publishing.force_private`) — and still locked private by YouTube's
      own audit gate regardless, per docs/LIMITS.md §2
- [x] Idempotent retry: `db.publication_status` is checked before any upload
      is attempted, so a clip already PUBLISHED to a platform is never
      re-attempted on it — see docs/LIMITS.md §9b for the granularity this
      actually holds at (per platform, not per byte; nothing can resume a
      byte-level upload across a process restart since the file itself does
      not persist between runs)

The compliance audit itself is tracked under "Blocking, not code" above, not
here — it is not a coding task.

**Not verified against a live account.** No YouTube OAuth client was
available to test with — see docs/LIMITS.md §9 before trusting this the way
every other phase's measurements have been earned.

## Phase 7 — Instagram ✅ (code complete; unverified against a live account)

- [x] Publish the rendered file to a public HTTPS URL for `video_url`: a
      temporary GitHub Release asset (`src/publishing/hosting.py`), reusing
      one release across every clip and deleting the asset again once
      Instagram's container finishes — never committed to git history
- [x] Container create → poll status_code → publish, with the 90s Reels cap
      comfortably inside this project's 20-60s clip range (enforced upstream
      by `clip.max_duration`, not re-checked here)

**Not verified against a live account.** No Instagram Professional account,
Meta app, or GitHub write token was available to test with — see
docs/LIMITS.md §9/§9a.

## Phase 8 — TikTok

- [ ] OAuth + Content Posting API, expecting `SELF_ONLY` pre-audit
- [ ] Start the audit

Deliberately not built alongside Phase 6/7 — out of scope for now by
explicit request, not a technical blocker. `publish` skips TikTok
unconditionally regardless of `publishing.tiktok` in config, same as
`status` already reports it as not implemented.

## Phase 9 — Full automation ✅

- [x] `daily_clip.yml` — `publish.yml` renamed, now backed by real
      enforcement rather than just a cron cadence: `db.clips_published_today`
      vs `publishing.max_per_day`, checked before anything renders; a clip
      already PUBLISHED on one platform today may still finish a second
      platform past the cap (`db.clip_has_publication_today`), since that is
      not a new clip and does not spend a second slot
- [x] Cross-video selection: `db.videos_ready_for_publish` orders every
      ANALYZED video by its own best CANDIDATE clip's score (spec section
      12), so the day's slot goes to the best clip anywhere, not just the
      best one in whichever video sorts first by recency
- [x] `retry` command for failed jobs — now resumes each video at the
      latest stage still on record (ANALYZED if a CANDIDATE clip remains,
      TRANSCRIBED if only the transcript survives, DISCOVERED otherwise)
      instead of unconditionally restarting the whole pipeline
- [x] Manual approve/skip/override of candidates and timestamps: `clips
      VIDEO_ID` lists a video's clips, `approve-clip CLIP_ID` forces one to
      the front of its video's render order, `skip-clip CLIP_ID` rejects
      one manually, `edit-clip CLIP_ID --start S --end S` retimes one —
      all four refuse anything that is not still CANDIDATE, since a
      RENDERED/PUBLISHED clip's record is history (LIMITS.md §9b)
- [x] Status summary in the Actions run page for the whole pipeline — this
      already existed (every workflow's final step runs `status`, which
      covers every stage, not just its own); the one dishonest line
      (`_todays_clip_state`) now reports the real cap instead of a
      placeholder

**Deliberately not done:** a title override (spec section 29 asks for
start/end and approve/skip; title generation has no CLI override — same gap
`docs/SPEC-CONFORMANCE.md` section 29 already tracked before this phase).
Operator notification on free-tier exhaustion (§25) also remains on GitHub's
own failed-workflow email, not addressed here — it was never one of this
phase's three checklist items above, despite an earlier doc cross-reference
suggesting otherwise (now corrected).
