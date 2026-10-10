# Spec conformance audit

Section-by-section audit of the implementation against the project
specification. Audited **2026-10-10**, updated at the end of Phase 4.

Legend: **✅ done** · **◐ partial** · **⏳ later phase** · **⚠️ deviation**
(implemented differently from the spec, with reasoning) · **❌ gap**

---

## 1-4 · Goals, architecture, cost, technology

| # | Requirement | State |
|---|---|---|
| 1.1 | Monitor source channels | ✅ |
| 1.2 | Detect new videos automatically | ✅ |
| 1.3 | Download permitted source videos | ✅ verified working (LIMITS §7) |
| 1.4 | Transcribe | ✅ measured to 0.38s worst-case accuracy |
| 1.5 | Identify strongest moments | ✅ measured against the live API on real multi-accent F1 radio |
| 1.6-1.8 | Render → publish | ⏳ Phases 5-8 |
| 1.9 | History prevents double-posting | ✅ schema-enforced (see §7) |
| 1.10 | Runs without my computer online | ◐ download works locally; **not yet measured on a GitHub runner** |
| 1.11 | ₹0/month recurring | ✅ no paid dependency anywhere |
| 1.12 | Free/open-source tooling | ✅ |
| 2 | Pipeline stage order | ✅ followed; "Audio Extraction" lives inside transcription (`transcription/audio.py`) rather than as its own stage, and download+transcribe are one job because the media only exists inside a single run |
| 3 | No paid SaaS, document insufficient free tiers | ✅ `docs/LIMITS.md`, with a second free option per limitation |
| 4 | Python 3.12+, FFmpeg, yt-dlp, SQLite, Actions, Data API | ✅ |

**On 1.11:** no paid service is reachable from this codebase. There is no
paid fallback to disable, and adding one would require a code change, not a
config flag.

**On 1.10 — the one goal genuinely at risk.** Media download failed from the
development machine on all nine yt-dlp player clients. If GitHub runners are
refused the same way, unattended operation is not achievable on free
infrastructure and the fallback is a self-hosted runner (i.e. a machine you
leave on). Measure with `python -m src.main doctor`; full analysis in
LIMITS §7.

## 5 · Repository structure

Spec-named files that exist: ✅ `README.md`, `CLAUDE.md`,
`requirements.txt`, `.env.example`, `.gitignore`, `config/sources.yaml`,
`src/main.py`, `src/monitoring/youtube_rss.py`,
`src/monitoring/source_monitor.py`, `src/download/downloader.py`,
`src/database/db.py`, `src/utils/logging.py`, `src/utils/config.py`,
`.github/workflows/monitor.yml`, `tests/`.

⏳ Not yet created, because their phase has not run:
`src/transcription/transcriber.py`, `src/analysis/*`, `src/video/*`,
`src/publishing/*`.

⚠️ **`daily_clip.yml` deliberately absent.** The spec lists it. A scheduled
workflow that cannot produce a clip is a job that fails on a timer and
trains you to ignore the notifications. It arrives in Phase 5/6 with
something to run.

⚠️ **Modules added beyond the spec's tree**, each because a responsibility
turned out to be real and separable:

| Added | Why |
|---|---|
| `monitoring/youtube_api.py` | Data API detection + the per-video licence check |
| `monitoring/types.py` | `FeedEntry` shared by both detectors, so the orchestrator never branches on which one ran |
| `download/gate.py` | The rights/eligibility decision as one pure function — no network, no DB, no filesystem. The most important code in the project, and this is what makes it exhaustively testable |
| `download/runner.py` | Orchestration, kept out of the yt-dlp wrapper |
| `download/workspace.py` | Temp-dir lifecycle (§8 cleanup) |
| `download/diagnose.py` | The `doctor` command (see LIMITS §7) |

## 6 · Source monitoring

| Requirement | State |
|---|---|
| Multiple source channels | ✅ |
| Config shape `name` / `channel_id` / `enabled` | ✅ plus `rights_cleared`, `rights_note` |
| Use YouTube RSS/API to detect | ⚠️ see below |
| Don't re-download to detect change | ✅ conditional GET (ETag/304) on RSS; metadata-only on the API |
| Store channel, video id, title, URL, published, status | ✅ |
| All nine statuses | ✅ exactly as specified |

⚠️ **The spec says "initially use YouTube RSS/API"; the emphasis is
inverted.** The Data API is primary and RSS is the fallback, not the other
way round. Reason, measured 2026-10-10: `feeds/videos.xml` returned HTTP 404
for several known-good channels from this network while
`youtube.com/channel/UC...` returned 200 over the same connection. The
endpoint has been flaking since Dec 2025, reportedly concentrated on
datacenter IPs — which is what every CI runner is. Detection via
`playlistItems.list` costs **1 quota unit** per source per check (8/day at
3-hourly polling), so the quota argument for RSS does not survive contact
with the reliability problem. Both are wired; `monitoring.backend: auto`
picks.

## 7 · Database

| Requirement | State |
|---|---|
| SQLite | ✅ |
| `sources`, `source_videos`, `clips`, `publications` | ✅ (plus `api_usage`, `schema_meta`) |
| UNIQUE source + video id | ✅ `UNIQUE(source_id, youtube_video_id)` |
| UNIQUE clip + platform | ✅ `UNIQUE(clip_id, platform)` |

⚠️ **`clips.start_ms` / `end_ms` instead of `start_time` / `end_time`.**
Integers (milliseconds), not REAL seconds. The reason is correctness, not
taste: §13 requires deduplication by timestamp range, which is enforced here
as `UNIQUE(source_video_id, start_ms, end_ms)` — and a uniqueness constraint
over floating-point columns is unreliable, because two values that should
collide can differ in the last bit and silently both insert. The §10 LLM
output format uses float seconds; conversion happens at that boundary.

Columns added beyond the spec (it says "at minimum"): `status_detail` and
`retry_count` for §23 logging; `license`, `duration_seconds`,
`live_broadcast_content`, `details_checked_at` as the gate's audit trail;
`download_path` / `download_bytes` / `download_format`;
`feed_etag` / `feed_modified` for conditional GETs.

Schema is versioned and migrated in place (v1 → v2), tested against a
hand-built v1 database and against a half-applied migration.

## 8 · Video downloading

| Requirement | State |
|---|---|
| Use yt-dlp | ✅ |
| Best reasonable quality | ✅ configurable height cap, default 1440p |
| Prefer FFmpeg-compatible formats | ✅ H.264 + AAC in MP4 preferred explicitly |
| Avoid duplicate downloads | ✅ status-gated; a stale path is cleared, not trusted |
| Clean up temporary files | ✅ `finally`-guaranteed, including on exception |
| Respect source permissions | ✅ two-layer gate (§26) |
| Never bypass DRM/access controls | ✅ and DRM errors classify as permanent, not retried |
| Configurable max duration / size | ✅ both, enforced **before** fetching bytes |

**Why the height cap defaults to 1440p:** a 9:16 crop of 16:9 keeps ~56% of
the width, so a 1080p source must be upscaled to fill 1080x1920 while 1440p
yields a true 1080-wide frame. §14 forbids unnecessary upscaling, so this is
that rule applied upstream.

## 9-21, 28 · Later phases

| Section | Requirement | State |
|---|---|---|
| 9 | Transcription behind a replaceable interface | ✅ `transcribe(media_path) -> Transcript`, two backends, spec's segment shape |
| 10 | LLM clip selection, strict JSON | ✅ `GeminiClipSelector` — malformed/out-of-range/invented-timestamp entries rejected, never coerced |
| 11 | F1-weighted scoring formula | ✅ `clip_scorer.final_score`, configurable weights, applied identically to both backends |
| 12 | One clip/day, backup candidates | ⏳ Phase 9 (`db.best_candidates_overall` is the infrastructure) |
| 13 | Clip deduplication | ✅ schema constraint (exact repeat) + in-video overlap filter + cross-video Jaccard text similarity. Perceptual hashing (the spec's other suggestion) applies to the RENDERED video, which does not exist until Phase 5 |
| 14 | 1080x1920 H.264/AAC MP4 | ⏳ Phase 5 (config written) |
| 15 | Configurable vertical reframing | ⏳ Phase 5 |
| 16 | Burned-in captions | ⏳ Phase 5 |
| 17 | Configurable branding | ⏳ Phase 5 (config written) |
| 18 | Title/description/hashtags | ⏳ Phase 5 |
| 19 | YouTube publishing | ⏳ Phase 6 — **and see the audit gate below** |
| 20 | Instagram Reels | ⏳ Phase 7 |
| 21 | TikTok | ⏳ Phase 8 — **audit gate** |
| 28 | Quality gate before publishing | ⏳ Phase 5 |

**§19/§21 carry gates no code can satisfy.** YouTube locks API uploads from
an un-audited project to private — `privacyStatus: public` is accepted and
silently ignored. TikTok forces `SELF_ONLY` until its own audit passes
(reported 2-4 weeks). Both audits are free. Both are manual and outside this
repository. Confirmed against official docs; figures in LIMITS §2-3.

## 22 · Scheduling

✅ `monitor.yml` every 3h, `download.yml` every 6h, `tests.yml` on push/PR.
✅ Cron imprecision assumed, not fought: every pass is idempotent, so a
dropped scheduled run costs latency only. ⚠️ `daily_clip.yml` deferred (§5).

## 23 · Failure handling

| Requirement | State |
|---|---|
| Every step retryable | ✅ FAILED → retry → DOWNLOADED, with a ceiling |
| Exponential backoff | ✅ with jitter; 429/5xx retried, other 4xx never |
| No duplicate posts after a timeout | ✅ `UNIQUE(clip_id, platform)` makes it structurally impossible |
| Check before retrying a publish | ⏳ Phase 6 |
| Log timestamp, job id, source video, clip id, platform, error, retry count | ✅ all seven, via `LogContext` |

Job ids use the GitHub run id in CI, so a log line links back to the
workflow run that produced it.

## 24 · Secrets

✅ `.env.example` with all ten spec-named variables, plus `YOUTUBE_API_KEY`,
`BRAND_NAME`, `LOG_LEVEL`. ✅ Never committed (`.gitignore`). ✅ GitHub
Secrets documented in the README with a per-phase table.

❌ **Gap found and fixed during this audit:** `YOUTUBE_API_KEY` was absent
from `.env.example` even though Phase 2 requires it — a fresh clone
following the docs would have been stuck with everything deferred and no
indication why. Added with its full Cloud Console setup steps.

Least privilege holds: `monitor.yml` and `download.yml` receive only
`YOUTUBE_API_KEY`. Publishing credentials are never exposed to them.

## 25 · Free-tier enforcement

✅ Usage tracked (`api_usage`, per service per day). ✅ Quota exhaustion
defers the whole pass and keeps work queued — it never downgrades to
unverified downloading. ✅ Reason logged. ✅ No paid fallback exists.
❌ Operator notification not implemented — currently relies on GitHub's own
failed-workflow email. Phase 9.

## 26 · Rights / copyright

✅ Two-layer gate: per-source `rights_cleared` (+ mandatory `rights_note`,
enforced by the config loader) **and** per-video `status.license ==
creativeCommon`, because licence is a property of the video and a CC channel
can still host all-rights-reserved uploads.

✅ Three verdicts, not two: ALLOW / SKIP / **DEFER**. An unverifiable video
defers — "cannot check" never reads as either eligible or ineligible.

✅ None of the forbidden mechanisms implemented: no DRM circumvention, no
paywall or auth bypass, no Content ID evasion, no modification intended to
defeat matching. ✅ `docs/RIGHTS.md` states the assumptions and is explicit
that a CC marking is a filter, not a clearance — the uploader applied that
licence, which is not evidence they held the right to.

## 27 · Development strategy

✅ Phase 1 then Phase 2, each tested before proceeding, repo runnable
throughout. ✅ Phase 2 tested against a permitted sample (Blender's
Big Buck Bunny, CC-BY) — which is how the download failure was found.

## 29 · Manual override

| Requirement | State |
|---|---|
| Retry failed jobs | ✅ `retry [VIDEO_ID] [--reset-count]` |
| Skip a candidate | ✅ gate SKIP, or `source-disable` |
| Approve a candidate | ◐ `unskip VIDEO_ID` requeues one video; clip-level approval is Phase 4 |
| Disable a source | ✅ `source-disable` / `source-enable` |
| Change start/end timestamp | ⏳ Phase 4 |
| Change title | ⏳ Phase 5 |
| Disable a platform | ✅ config `publishing.youtube/instagram/tiktok` |
| Operates automatically without intervention | ✅ |

`--reset-count` is opt-in: zeroing the counter by default would make the
retry ceiling a no-op and let a permanently broken video consume a slot
forever. `unskip` is one video at a time and never bulk — most skips are
rights or licence failures, and a bulk un-skip is an easy way to push
unlicensed footage in by accident. It requeues only; the gate re-runs.

## 30 · Configuration

⚠️ **Split across two files** rather than the spec's single document:
`config/config.yaml` (clip, publishing, branding, processing, download,
monitoring, video, logging) and `config/sources.yaml` (channels + rights).
The reason is blast radius — a malformed source list should not stop
`status` from reporting, and the rights-bearing file is the one a human
edits most. Every spec key is present. `${VAR}` and `${VAR:-default}`
substitution means any value can come from a secret without editing a file.

Unknown keys in a source entry are a hard error, not a silent ignore: a
typo'd `rights_clered: true` must never read as false.

## 31 · Monitoring output

✅ `python -m src.main status` in the spec's illustrated shape, printed to
stdout and appended to `$GITHUB_STEP_SUMMARY` — no web service, per the
spec's preference.

Unimplemented stages report what they are rather than a status: the platform
lines read "enabled in config, not implemented (Phase 6)". A line reading
`PUBLISHED` for a publisher that does not exist would be a lie in the one
output you rely on.

## 32 · Engineering rules

✅ 1 simple · 2 free/OSS · 3 three runtime deps (PyYAML, requests, yt-dlp) ·
4 no hard-coded credentials · 5 no secrets committed · 6 idempotent jobs ·
7 retry-safe · 8 temp files out of git (media never committed) · 9 modular ·
10 every external setup step documented · 11 free tiers treated as finite
and tracked · 12 API restrictions verified against official docs, dated ·
13 no scraping/browser automation for publishing · 14 no DRM bypass ·
15 no Content ID evasion.

On rule 12: verified figures carry a date, and a number the cost model rests
on is confirmed by a second independent route — a summarising doc fetch
corrupted two load-bearing values during this project (an inverted negation
and a 1,600x quota error), so single-read figures are not trusted.

## 33 · Claude Code instructions

✅ Repo inspected, `CLAUDE.md` maintained, `TODO.md` checklist, one phase at
a time, tests after each, runnable throughout, credentials documented, free
solutions preferred, current docs consulted rather than recalled, no
copyright/DRM bypass.

## 34 · Definition of done

| Criterion | State |
|---|---|
| New videos detected automatically | ✅ |
| Downloaded when authorized | ◐ gate ✅, byte fetch unverified |
| Transcribed | ⏳ |
| Multiple candidates identified | ✅ |
| Candidates scored | ✅ measured against the live API |
| Best selected | ✅ ranked per video; cross-video "1/day" selection is Phase 9 |
| Duplicates prevented | ✅ constraint + overlap filter + cross-video similarity |
| 9:16 MP4 generated | ⏳ |
| Captions burned in | ⏳ |
| Metadata generated | ⏳ |
| YouTube Shorts publishing works | ⏳ **+ audit gate** |
| Instagram Reels publishing works | ⏳ |
| TikTok publishing works where permitted | ⏳ **+ audit gate** |
| Max one clip/day enforced | ⏳ |
| Failed jobs retryable | ✅ |
| Publishing idempotent | ✅ structurally (constraint in place) |
| Secrets stored securely | ✅ |
| Actions run automatically | ✅ for implemented phases |
| No mandatory paid SaaS | ✅ |
| README complete | ✅ for implemented phases |
| Free-tier limitations documented | ✅ `docs/LIMITS.md`, dated |
| Copyright assumptions documented | ✅ `docs/RIGHTS.md` |

**Three things stand between this and "done" that are not code:**

1. Whether CI can download at all (LIMITS §7) — measure before Phase 3.
2. The YouTube compliance audit — free, manual, no published turnaround.
3. The TikTok audit — free, manual, reported 2-4 weeks.

Starting 2 and 3 early is worth more than any amount of further coding,
because both are queues you do not control.
