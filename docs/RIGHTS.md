# Rights, copyright and what this software does not do

## The assumption this project is built on

**The operator holds the rights or permission to repurpose every source they
enable.** The software cannot verify that and does not try. It makes the
assumption explicit and auditable instead: every source carries a
`rights_cleared` flag and a `rights_note`, and the pipeline refuses to
download, render or publish anything from a source where the flag is false.

Discovery is the one exception — an un-cleared source is still monitored, and
its videos are recorded at status `SKIPPED` with the reason attached. Recording
a skip is what stops the same video being reconsidered on every later pass.
Nothing is downloaded.

## What `rights_cleared: true` is meant to mean

Set it only when at least one of these is true:

- the channel is **yours**;
- the footage is published under a licence permitting reuse (e.g. a Creative
  Commons licensed YouTube upload — YouTube exposes this per video);
- you hold **written permission** from the rights holder;
- the footage is **licensed to you** under an agreement that covers
  short-form redistribution on the platforms you publish to.

`rights_note` is not decoration. It is the audit trail: a sentence saying
*which* of the above applies. The config loader rejects
`rights_cleared: true` with an empty note, so the claim cannot be made
silently.

## A Creative Commons marking is a filter, not a clearance

This project's sources are CC-licensed / licensed footage, so one failure
mode deserves naming on its own.

`status.license == "creativeCommon"` on a YouTube video means **the uploader
applied that licence**. It does not mean the uploader held the right to apply
it. Re-uploads of broadcast footage marked Creative Commons are common, and
the marking is simply invalid on them — a re-uploader cannot license away
someone else's copyright, and the rights holder's position is untouched by
what was ticked at upload time.

So the automated per-video licence check (Phase 2) is a **necessary filter
that removes obviously unusable videos**. It is not evidence that a video is
safe to repurpose. The judgement that remains yours: is this uploader
plausibly the owner of what they uploaded? `rights_note` should answer that
— who the uploader is and why their licence claim is credible. "It was
marked CC" is not an answer.

### Measured evidence, 2026-10-10

This is not a theoretical caution. Querying the Data API for F1 videos with
`videoLicense=creativeCommon` returned, in the first five results, titles
like:

```
creativeCommon   981s   "Antonelli STRIPPED Of Madrid GP Win As Shock FIA DQ Sparks..."
creativeCommon    28s   "Is this the craziest F1 pre-race week ever? #..."
creativeCommon    37s   "A REGRA DO PESO NA F1 É CRUEL!"
creativeCommon    27s   "herbalife formula 1 weight loss #shorts"
```

Run through the gate, the first of those returns **ALLOW**. It is a
broadcast-derived F1 news/commentary upload whose uploader ticked Creative
Commons. They are not in a position to license FOM's footage, so the flag
carries no weight — but the pipeline cannot tell, and will accept it.

The fourth result is a weight-loss advert that matched on the words "formula
1", which is a useful reminder that `videoLicense=creativeCommon` selects for
a tickbox and nothing else.

**The practical consequence for this project:** do not populate
`config/sources.yaml` by searching for CC-licensed F1 content. The CC filter
is there to stop obvious mistakes within a channel you have already
evaluated; it cannot do the evaluating. Pick channels on the strength of who
runs them, record why in `rights_note`, and let the per-video check catch
the exceptions inside an otherwise-sound source.

Use `python -m src.main check VIDEO_ID...` to see exactly what the gate
decides for specific videos, and why, before enabling a source.

## Official Formula 1 footage

Formula 1 race broadcasts, onboards, team radio and official highlight
packages are owned by Formula 1 / FOM and their broadcast partners. They are
not licensed for third-party short-form redistribution, and they are among the
most aggressively Content-ID-matched footage on the platform.

**Do not add F1's own channels, broadcast re-uploads, or aggregator channels
that re-host broadcast clips.** No amount of editing changes the ownership.
The shipped `config/sources.yaml` contains a single disabled, un-cleared
placeholder for exactly this reason: a default source list is an implicit
recommendation, and this project makes none.

What an F1-focused channel can legitimately be built on: your own commentary
and analysis, your own sim-racing footage, interviews you conducted or
licensed, footage under a reuse licence, and content you have explicit
permission for.

## What this software deliberately does not implement

Per spec §26, and this is a design boundary, not a TODO:

- DRM circumvention
- paywall or authentication bypass
- any circumvention of access controls or copyright protection
- Content ID evasion
- visual or audio modification whose purpose is to defeat automated
  copyright matching (no pitch shifting, mirroring, speed nudging, border
  noise or frame-edge cropping introduced for that reason)
- disguising copyrighted footage as original work

The reframing, caption and branding stages exist to make a clip *watchable
vertically*, and they are configured for that. If a future change to them
would only make sense as a detection-evasion measure, that is the signal that
it does not belong in this codebase.

## What this document is not

This is not legal advice, and the software makes no claim that editing
footage makes its reuse lawful. "Fair use" / "fair dealing" is a
jurisdiction-specific defence assessed case by case, not a setting. If the
rights position of a source is unclear, the correct value of
`rights_cleared` is `false`.
