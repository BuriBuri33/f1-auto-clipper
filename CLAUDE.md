# CLAUDE.md — working in this repository

## What this is

An automated short-form clipping pipeline (detect → download → transcribe →
select → render → publish), built to run on free tiers in GitHub Actions at a
target of ₹0/month recurring, producing one clip per day.

Read [docs/LIMITS.md](docs/LIMITS.md) and [docs/RIGHTS.md](docs/RIGHTS.md)
before changing anything in `monitoring/`, `publishing/` or `video/`. They
hold verified figures and hard boundaries that are easy to violate by
accident.

## Decisions already taken (do not re-litigate)

- **The repository is PUBLIC.** That choice buys unmetered Actions minutes on
  a 4 vCPU / 16 GB runner and the free public HTTPS hosting that Instagram's
  `video_url` requires. Consequences to respect: never write anything
  sensitive into `config/`, the state database or a log line, because all
  three are world-readable. Credentials come from GitHub Actions secrets,
  which are not exposed to fork-PR workflows.
- **Sources are Creative Commons / licensed footage**, not the operator's own
  uploads. This makes the rights gate the most important code in the
  repository rather than a formality — see "Rights verification" below.

## Rights verification (Phase 2 requirement)

Because sources are CC/licensed rather than self-owned, `rights_cleared` on
the *source* is necessary but not sufficient: licence is a property of each
**video**, not of the channel. A CC channel can and does contain
all-rights-reserved uploads.

So Phase 2 must verify per video, not per channel:

- `videos.list?part=status` returns `status.license`, either
  `creativeCommon` or `youtube`. It costs **1 quota unit**, so checking every
  discovered video is effectively free.
- A video whose licence is not `creativeCommon` is set to `SKIPPED` with the
  reason recorded, even when its source is rights-cleared.
- The check belongs at the download boundary as well as at discovery. Two
  checks, because the gate is the whole safety story here.

**The limit of that check, stated plainly:** a CC marking means the
*uploader* applied that licence. It does not mean the uploader had the right
to. Re-uploads of broadcast footage marked Creative Commons are common, and
the marking is invalid on them — the rights holder's position is unaffected
by what a re-uploader ticked. So the automated check is a necessary filter,
never a clearance. `rights_note` must say who the uploader is and why their
licence claim is credible; "it was marked CC" is not an answer on its own.
See `docs/RIGHTS.md`.

## Phase discipline

Implement one phase at a time; do not start the next while the previous is
broken. The repository must be runnable after every meaningful change — that
means `python -m pytest tests -q` green and `python -m src.main status`
working. Current state and the full checklist: [TODO.md](TODO.md).

A subcommand is registered in `src/main.py` only once it does something. Do
not advertise an unimplemented phase in `--help`, and do not add a scheduled
workflow for a phase that cannot run.

## Hard rules

1. **No paid dependency.** Not a paid SaaS, not a paid API tier, not a
   "free trial". If a free option is insufficient, document the limitation in
   `docs/LIMITS.md` and find a second free option. Never add a paid fallback
   without an explicit configuration switch that defaults to off.
2. **Verify, do not recall, any external service's limits.** Figures move.
   `docs/LIMITS.md` carries a verification date; update it when you re-check.
   When reading vendor docs, ask for the verbatim clause rather than a
   summary — a summarised read has already corrupted both a negation and a
   quota table in this project's history.
3. **No rights or copyright circumvention.** No DRM bypass, no paywall or
   auth bypass, no Content ID evasion, no edit whose purpose is to defeat
   automated matching. See `docs/RIGHTS.md` — this is a boundary, not a
   backlog item.
4. **Never commit secrets.** Credentials come from the environment, via
   `${VAR}` substitution in config or `os.environ` directly. `.env` is
   gitignored. The `monitor` workflow receives only the secrets it needs.
5. **Idempotency belongs in the schema.** Duplicate prevention is enforced by
   UNIQUE constraints, not by "check then insert" — the check races, the
   constraint does not. Any new stage that can be retried gets a constraint.
6. **Every external call is retry-safe.** Exponential backoff with jitter;
   retry 429 and 5xx; never retry a 4xx that will fail identically. Before
   retrying a *publish*, check whether the platform already created the post.
7. **Fail gracefully on a spent free tier.** Log the reason, leave the work
   queued, do not escalate. `api_usage` tracks consumption.

## Conventions

- **Python 3.10+ is a hard floor; 3.12 is the target and what CI runs.**
  Current yt-dlp refuses to install below 3.10, so the earlier
  "keep it 3.9-compatible" convention is obsolete and was removed — it is
  what kept a year-stale yt-dlp in place long enough to look like a network
  block. The existing `from __future__ import annotations` style is harmless
  and can stay; do not add new 3.9 constraints.
- **Dependencies**: minimal. Prefer the standard library. `requirements.txt`
  lists what the implemented phases need and comments what later phases will
  add — do not install ahead of a phase.
- **Config**: `config/config.yaml` for pipeline settings, `config/sources.yaml`
  for channels. Both support `${VAR}` and `${VAR:-default}`. Unknown keys in a
  source entry are an error, not a silent ignore — a typo in
  `rights_cleared` must not read as false.
- **Errors**: one bad source must never abort a pass over the others. Return
  a per-source result with the error attached; fail the run only if every
  source failed.
- **Logging**: `src/utils/logging.py`. Bind a `LogContext` once per job and
  use `log.bind(...)` / `fields={...}` for per-step detail. The standard
  fields are job id, source video, clip id, platform, retry count.
- **Tests**: no network. Inject a fake session; the detectors all take a
  `session` parameter for this. Test the *decisions* (does the rights gate
  hold on both backends? is a second pass a no-op?), not the plumbing.
- **Time**: UTC everywhere, `db.utcnow()`, ISO 8601 to the second.

## Layout

```
config/            config.yaml (settings), sources.yaml (channels + rights)
src/main.py        CLI
src/monitoring/    types.py (shared FeedEntry), youtube_api.py (primary),
                   youtube_rss.py (fallback), source_monitor.py (orchestration)
src/database/db.py schema, status enums, all SQL
src/utils/         config.py, logging.py
src/download/      Phase 2    src/transcription/  Phase 3
src/analysis/      Phase 4    src/video/          Phase 5
src/publishing/    Phases 6-8
data/state.db      committed state — the pipeline's memory across CI runs
docs/              LIMITS.md (verified figures), RIGHTS.md (boundaries)
```

## Things that have already bitten

- **The YouTube Atom feed is not dependable.** Intermittent 404s since
  Dec 2025, concentrated on datacenter IPs — i.e. CI runners. Verified 404 on
  known-good channels from this machine while channel pages returned 200.
  Hence the Data API is primary and RSS is the fallback, not the reverse.
- **YouTube locks API uploads private until the API project passes a free
  compliance audit**, silently — `privacyStatus: public` is accepted and
  ignored. TikTok does the same with `SELF_ONLY` until its own audit passes.
  Neither is a code problem.
- **Instagram's API does not accept file uploads**; it fetches a public HTTPS
  `video_url`. And Reels via API are capped at 90 seconds.
- **Groq's transcription limit that bites is the 25 MB file cap**, not the
  daily seconds. Long sources are chunked, each chunk encoded directly from
  the source at a bitrate we choose — never cut out of an intermediate file,
  and never sized from a measured average (a mostly-silent file averages far
  below the rate its speech-dense chunks actually encode at).
- **Probe the AUDIO stream's duration, not the container's.** They differ
  (observed 30.60s vs 19.32s), and planning from the container puts chunks
  past the end of the audio, where FFmpeg writes files with no frames.
- **Word timestamps are not optional.** Without them Whisper's segment END
  times are wrong by seconds — they stretch across following silence. And
  Whisper's segment *grouping* varies with the audio encoding, so this
  project rebuilds segments from word timings rather than trusting it.
- **Groq puts words at the TOP LEVEL of the response, not inside each
  segment.** Reading `segment["words"]` finds nothing and silently loses all
  word timing; the words must be distributed into the segments. Measured
  effect: 2.00s/3.32s worst-case error becomes 0.08s/0.48s.
- **faster-whisper does not pin PyAV, and current PyAV breaks its decoding.**
  Audio is decoded with the FFmpeg binary and passed as a numpy array; never
  hand faster-whisper a file path.
- **Download and transcription are one pass (`ingest`).** The media file
  exists only inside a single workflow run — too large to commit, and the
  runner is destroyed. Splitting them would mean downloading twice.
- **A stale yt-dlp is indistinguishable from an IP block.** 2025.10.14
  failed to fetch media on all 9 player clients with 403s and "The page needs
  to be reloaded"; 2026.08.19 succeeded on the first client, same machine,
  minutes later. Symptom points at the network, cause is the dependency.
  Guarded three ways now: a floor in `requirements.txt`, a `doctor` check
  that runs before everything else, and `--upgrade yt-dlp` in the workflow.
  Check the version before investigating any download failure.
- **yt-dlp extraction still breaks per client**, and differently for metadata
  than for media — hence the fallback chain. Never pin yt-dlp: only a newer
  release fixes a YouTube change.
- **A single client reporting "Video unavailable" does not mean the video is
  unavailable** — it routinely means that client cannot see it. A terminal
  SKIP therefore requires every client to agree; a mixed chain stays
  retryable. This cost a real bug: the first implementation aborted on the
  first permanent-looking error and would have dropped good videos forever.
- **YouTube serves one progressive format and it is 360p.** Anything usable
  for a 1080x1920 render must be muxed, so FFmpeg is a Phase 2 requirement,
  not a Phase 5 one.
- **The YouTube `license` field is absent from yt-dlp output**, even for a
  known CC-BY video. The licence decision comes from the Data API's
  `status.license` and from nothing else.
