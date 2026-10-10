# Free-tier limits and platform gates

Everything here was verified on **2026-10-10**. Re-check before relying on it:
platform terms move, and two of the findings below are the kind that change
without an announcement.

Nothing in this project requires a paid subscription. Two things do require a
**free but manual approval** from the platform, and they are the difference
between "the pipeline posts publicly" and "the pipeline posts privately to an
audience of one". Read §2 and §3 before building a content plan on this.

---

## 1. Compute and storage — GitHub Actions

| | Public repo | Private repo |
|---|---|---|
| Standard runner minutes | **free, unmetered** | 2,000/month (Free plan) |
| Runner size (Linux) | 4 vCPU / 16 GB | 2 vCPU / 8 GB |
| Artifact storage | free | 500 MB |
| Actions cache | 10 GB/repo | 10 GB/repo |

**This is the single biggest cost lever in the project.** A public repository
gets unmetered minutes on a bigger runner, which makes local Whisper
transcription viable as a fallback (§4) and removes any need to ration runs.
A private repository gets 2,000 minutes/month ≈ 66 minutes/day, which is
enough for one clip/day *provided* transcription is API-based, but leaves no
headroom for re-runs.

Larger runners are metered even on public repositories — this project never
requests one.

Trade-off of going public: the repo contents are world-readable, which
includes `config/sources.yaml` and the state database (what you monitor, what
you clipped, when). It does **not** include secrets: GitHub Actions secrets
are encrypted and are not exposed to workflows triggered by forked PRs.

**Scheduled runs are not punctual.** GitHub explicitly does not guarantee
cron timing and drops scheduled runs entirely under load. Every job here is
idempotent for that reason: a missed run costs nothing but latency.

## 2. YouTube — the audit gate ⚠️

- **Data API quota: 10,000 units/day, free.** No card, no paid tier for this
  project's volume.
- `playlistItems.list` — **1 unit** per call (up to 50 items). This is the
  detector. Polling 1 source every 3 hours costs **8 units/day**.
- `search.list` — **100 units** per call. Deliberately never used.
- `videos.insert` (upload) — **1,600 units** per call. One upload/day = 1,600
  units, so detection plus one daily publish uses well under 20% of the free
  quota.
- Quota resets at **midnight Pacific**, not UTC.

**⚠️ The gate:** videos uploaded via `videos.insert` from an **un-audited API
project** (any project created after 28 July 2020) are **locked to private
viewing mode**. Sending `privacyStatus: public` does not fail and does not
help — the upload succeeds, and the video is locked private anyway. There is
no appeal for videos already locked; they must be re-uploaded through an
audited project or by hand.

To lift it, the API project must pass a **compliance audit** via the *YouTube
API Services – Audit and Quota Extension Form*. The audit is free. There is no
published turnaround time.

**What this means for this project:** Phase 6 ships with
`privacyStatus: private` deliberately, because that is the only honest
setting before the audit passes. Automated *public* YouTube Shorts are
blocked on a human approval that is outside this codebase's control. Plan for
it early; it is not a code problem.

## 3. Instagram and TikTok

### Instagram Reels (Meta Graph API)
- Requires an Instagram **Professional** (Business or Creator) account.
- **No direct file upload.** The API fetches the video from a `video_url` you
  supply, which must be a publicly reachable HTTPS URL. A public GitHub
  repository (raw file or a release asset) satisfies this at no cost — one
  more reason §1 points at a public repo.
- **Reels via API are capped at 90 seconds.** The clip target of 20–60s is
  comfortably inside this, but it is a hard ceiling on the format.
- Publishing limit: Meta's own docs state 50 posts/24h in one place and 100 in
  another. The authoritative answer per account is the
  `content_publishing_limit` endpoint. At 1 clip/day this is irrelevant.

### TikTok Content Posting API ⚠️
- Requires a developer app **and a separate audit** beyond signup.
- **Until that audit passes, every post is forced to `SELF_ONLY`** — visible
  only to the posting account — regardless of the visibility requested.
- Unaudited clients: max 5 users posting in any 24h window.
- Reported audit turnaround: 2–4 weeks, typically with several rounds of
  feedback.

**So: two of the three target platforms gate public automated posting behind a
free manual review.** Neither can be worked around in code, and this project
will not try to (see `RIGHTS.md`). Phase 7 and Phase 8 are built and tested
against the private/sandbox path, which is what the platforms permit before
approval.

## 4. Transcription — implemented, with findings

Verified by implementation on 2026-10-10. Four things bit, all of them
invisible to mocked tests:

**a. faster-whisper's PyAV dependency is unpinned and currently broken.**
A clean install gives faster-whisper 1.2.1 + PyAV 19.0.1, and *every*
transcription dies with
`open() got an unexpected keyword argument 'metadata_errors'`. Pinning PyAV
older is not a fix — no wheel on every platform, then it wants FFmpeg headers
to build. Resolved by decoding with the FFmpeg binary this project already
requires and handing faster-whisper a numpy array instead of a path. PyAV is
still installed transitively and simply never used.

**b. Chunk sizes overshot the 25 MB cap.** The planner sized chunks from the
*measured* average bitrate of the extracted file; the encoder then produced
chunks at the *nominal* bitrate. A file averaging 2,544 B/s yielded a chunk
at 4,040 B/s — 43% over the prediction, and over the cap. Fixed by sizing
from the bitrate we ask for, and by encoding each chunk directly from the
source media rather than cutting an intermediate file (which also drops the
audio from two lossy generations to one).

**c. Chunks were planned past the end of the audio.** `ffprobe` was asked for
the *container* duration; a file reporting 30.60s carried only 19.32s of
audio. The chunk covering 21.75–30.60s came out as a 224-byte MP3 with no
frames, which no decoder will open. Fixed by probing the audio stream's
duration, and by dropping any chunk too small to contain frames (the last
surviving chunk's window then extends to the end).

**d. Without word timestamps, segment end times are simply wrong.** Measured
against synthesised audio with known timing: an utterance genuinely ending at
3.68s was reported as ending at **7.78s**, stretched across the following
silence to meet the next utterance. With word timestamps on, the same segment
came back as 1.62–3.74. Spec section 9 requires accurate timestamps, so word
timestamps are on by default.

**e. Groq returns word timings at the TOP LEVEL of the response, not nested
inside each segment.** Verified against the live API. A parser that looks for
`segment["words"]` finds nothing, silently produces a transcript with no word
timings, and therefore cannot re-segment — leaving Groq's own boundaries in
place. Those boundaries are coarse: the same audio gave segments at
0.00–7.00, 7.00–13.00 and 13.00–19.00, rounded to whole seconds and stretched
across the silence between utterances.

Measured against known ground truth, before and after attaching the
top-level words:

| | worst start error | worst end error |
|---|---|---|
| Groq, words not attached | 2.00s | 3.32s |
| Groq, words attached and re-segmented | **0.08s** | **0.48s** |
| Local `faster-whisper` (`tiny`) | 0.38s | 0.19s |

The individual word timings were accurate to **0.02s** throughout — so the
information was always there, simply unread. `whisper-large-v3` (non-turbo)
behaves the same way, and also returns coarse segments.

Transcription quality note: Groq's `whisper-large-v3-turbo` transcribed "box
this lap" correctly where local `tiny` heard "box this lab" — as expected,
and a reason to prefer the API when a key is available.

A sixth, related finding shaped the design: **Whisper's segment grouping is
not stable.** The same speech and model gave 3 segments read from a WAV and
one 15-second segment read from the 32 kbit/s MP3 the pipeline produces —
with identical, correct word timings. So the pipeline ignores the backend's
grouping entirely and rebuilds segments from word timings, which makes
segmentation deterministic and under our control. It is also what later
phases need: clip selection must not start mid-sentence (section 10) and
captions must break into short readable phrases (section 16).

Measured accuracy after all of the above, against known ground truth:
**worst start error 0.38s, worst end error 0.19s**, including across three
forced chunk boundaries with zero duplicated speech.

## 4a. Transcription free tiers

**Primary — Groq, free tier** (`whisper-large-v3-turbo`), endpoint
`https://api.groq.com/openai/v1/audio/transcriptions` (OpenAI-compatible):
- 20 requests/min, 2,000/day
- **28,800 audio seconds/day** (8 hours)
- **25 MB max file size** ← the binding constraint

A 3-hour source exceeds 25 MB as a single file at any usable bitrate, so audio
is chunked (~20–30 min of mono 32–64 kbps Opus/MP3 per request) with segment
timestamps offset back to the source timeline. 3 hours of audio = 10,800s,
comfortably inside the 28,800s/day allowance for one video per day.

**Fallback — `faster-whisper` on the runner.** No API, no key, no quota. On a
public repo's 4-vCPU runner, the `base` model runs at roughly real-time or
better, so a 3-hour source is a long job but an unmetered one. This is the
reason the project does not depend on any transcription vendor staying free.

## 5. Clip selection LLM (Phase 4)

**Google AI Studio free tier** (`gemini-2.5-flash`): ~10 requests/min and
~1,500 requests/day on current reporting. Google no longer publishes a
guaranteed free-tier table — the live limits are per-project in the AI Studio
console, and developers report real limits below the published ones at peak
times. One clip-selection call per video per day sits far inside any of these
figures.

**Fallback:** the clip selector's interface takes a transcript and returns
ranked candidates, so a heuristic scorer (keyword + segment-density + pause
detection) can stand in with no API at all. Treat the LLM as an upgrade to
candidate *quality*, never as a dependency for the pipeline to run.

## 6. YouTube Atom RSS — why it is the fallback, not the primary

`https://www.youtube.com/feeds/videos.xml?channel_id=UC...` needs no
credentials and costs no quota, which would make it the perfect detector.

**It is not dependable.** It has returned intermittent 404s since
December 2025, and the failures are reported to concentrate on **datacenter
IPs** — which is exactly what a GitHub Actions runner is.

Verified from this machine on 2026-10-10: the endpoint returned **HTTP 404**
for several known-good channels (including the `?playlist_id=UU...` form)
while `https://www.youtube.com/channel/UC...` returned **HTTP 200** over the
same connection. So this is not a dead channel id and not a network outage —
the feed endpoint itself is unreliable.

Hence `monitoring.backend: auto`: Data API when `YOUTUBE_API_KEY` is set, Atom
feed otherwise and as a fallback when an API call fails. The two failure modes
are uncorrelated, which is the whole point of keeping both.

## 7. ✅ Downloading — diagnosed and working

**Resolved 2026-10-10.** This section previously recorded downloading as the
project's biggest open risk. It was a stale yt-dlp, not a blocked network.

The measurement, one machine, same video, minutes apart:

| yt-dlp | Media fetch |
|---|---|
| **2025.10.14** | FAILED on all 9 player clients |
| **2026.08.19** | **OK on the first client** — 12.9 MB through the pipeline's own code |

The failures the old version produced were `HTTP Error 403: Forbidden`,
`The page needs to be reloaded`, and `Video unavailable` — which is
**indistinguishable from being IP-blocked**. That is the trap: the symptom
points at the network and the cause is the dependency. Three hypotheses were
on the table (stale yt-dlp / IP reputation / PO-token gating); the first was
correct and the other two are not in play here.

Also resolved: the Atom-feed 404s in §6 are a separate, genuine YouTube-side
problem, so the two symptoms were **not** a common IP cause after all. The
Data-API-primary design stands on its own merits.

### What now guards this

1. **A version floor in `requirements.txt`** — `yt-dlp>=2026.8.19`. If an
   index cannot satisfy it, installation **fails loudly** rather than quietly
   producing a build that cannot download. That is deliberate.
2. **A `doctor` check** — "yt-dlp new enough" fails before anything else and
   names both upgrade routes, so this misdiagnosis cannot recur silently.
3. **`pip install --upgrade yt-dlp` in the `download` workflow**, every run.
   Never pin it: YouTube breaks extraction continuously and only a newer
   release fixes it.

### If the index is stale

The package index reachable from the original dev environment offered
2025.10.14 as "latest" — about a year old. Where that happens,
`pip install --upgrade yt-dlp` cannot help, and the fix is the source:

```bash
pip install --upgrade "yt-dlp @ git+https://github.com/yt-dlp/yt-dlp.git"
```

Note this requires **Python 3.10+**; current yt-dlp refuses to install on 3.9.

### Still unverified: a GitHub runner

Downloading is proven from a residential connection. It is **not yet proven
from a GitHub Actions runner**, whose IP is a datacenter address. The
cause being a dependency version rather than IP reputation makes CI very
likely to work — runners get a current yt-dlp from a real index — but
"likely" is not "measured".

```
Push, then read the `doctor` step in the download workflow run.
```

If it fails there and passes locally, IP reputation is back on the table and
the options are the ladder below. Until that run happens, treat CI
downloading as expected-to-work rather than known-to-work.

### If a runner genuinely cannot download

1. **Keep yt-dlp current** — already automated.
2. **A PO-token provider plugin.** Free, no account.
3. **A self-hosted runner** on a residential connection. Free, sidesteps IP
   reputation, but weakens "unattended" to "unattended while that machine
   is up".
4. **Browser cookies as a secret.** Listed last deliberately: it puts a live
   account credential in CI and risks the account being flagged. Not
   implemented, and not something to enable without accepting that. It is
   not an access-control bypass — it authenticates as you to fetch public,
   reuse-licensed videos — but it is still your account on the line.

All four keep the ₹0 target intact.

---

## Free-tier enforcement

Per spec §25, the pipeline tracks its own consumption (`api_usage` table) and
on exhaustion it **stops and logs** — it never escalates to a paid tier. There
is no paid fallback anywhere in the codebase, and adding one would require an
explicit configuration change, not a flag flip.
