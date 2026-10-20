"""The LLM prompt and JSON schema for clip candidate scoring.

Both were shaped by measurement against the live Gemini API on 2026-10-10,
not written from assumption:

* **An ambiguous "score 0-100" produced wild outliers** — the same objectively
  strong clip scored 9 on one call and 95 on a near-identical one. The
  rubric below gives explicit anchors for 0, 50 and 100 on every axis.
* **``responseSchema`` (structured output) is used rather than asking for
  JSON in free text** — more robust, and it is what makes rejection (not
  coercion) of a malformed entry possible: a field with the wrong type
  fails loudly instead of silently becoming ``str(None)``.
* **``thinkingConfig.thinkingBudget: 0`` matters.** Verified: with a
  ``responseSchema`` set, the *default* thinking budget spent 283-716 tokens
  "thinking" about a four-line transcript before answering, and took
  12-40s. Setting it to 0 explicitly gets a 0-token, few-second response
  with no measured quality loss on the cases tried. This is a latency/cost
  optimisation, not a correctness one — re-verify if quality looks off on
  real transcripts.
"""

from __future__ import annotations

from typing import Sequence

from ..transcription.types import Segment

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "clips": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start": {"type": "NUMBER", "description": "start time in seconds"},
                    "end": {"type": "NUMBER", "description": "end time in seconds"},
                    "hook_score": {"type": "INTEGER"},
                    "emotional_score": {"type": "INTEGER"},
                    "context_score": {"type": "INTEGER"},
                    "payoff_score": {"type": "INTEGER"},
                    "shareability_score": {"type": "INTEGER"},
                    "reason": {"type": "STRING"},
                    "hook": {"type": "STRING"},
                },
                "required": [
                    "start", "end", "hook_score", "emotional_score",
                    "context_score", "payoff_score", "shareability_score",
                    "reason", "hook",
                ],
            },
        },
    },
    "required": ["clips"],
}

# Verbatim rubric text, included in every prompt. Explicit anchors at 0/50/100
# on every axis — the thing that was missing when scores were unreliable.
SCORING_RUBRIC = """Score each candidate on five axes, each an integer 0-100:

hook_score — how strong is the opening moment?
  0 = flat, no reason to keep watching; 50 = mildly interesting;
  100 = an unmissable opening line or action in the first few seconds.

emotional_score — how much genuine emotion (excitement, shock, tension, joy,
  frustration) is present?
  0 = flat delivery, no emotion; 50 = some audible emotion;
  100 = raw, unmistakable emotional reaction.

context_score — can a viewer understand this WITHOUT having watched the rest
  of the video?
  0 = incomprehensible without outside context; 50 = mostly clear;
  100 = fully self-contained, needs nothing else.

payoff_score — does the clip resolve (an action completes, a question is
  answered, a result lands)?
  0 = goes nowhere, trails off; 50 = partial resolution;
  100 = a clear, satisfying resolution within the clip.

shareability_score — would someone send this to a friend with no caption
  needed?
  0 = nobody would share this; 50 = maybe, for the right audience;
  100 = obviously shareable, meme- or highlight-reel quality.

Do not select a clip SOLELY because it mentions a notable name or term —
score on what actually happens in the audio, not on keyword presence."""

CLIP_PRIORITY = """When several candidates are comparable, prefer, in this
order: (1) shocking or surprising reveals, (2) dramatic moments or
catastrophic events, (3) striking statistics or hard-to-believe facts,
(4) emotional human stories, (5) clear "aha" explanations of complex topics,
(6) controversial or counterintuitive claims, (7) funny or ironic moments,
(8) unexpected connections or implications, (9) strong interview moments,
(10) vivid scene-setting or atmosphere."""


def _format_segments(segments: Sequence[Segment]) -> str:
    lines = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            lines.append("[%.1f-%.1f] %s" % (segment.start, segment.end, text))
    return "\n".join(lines)


def build_prompt(
    windows,
    source_title: str = "",
    vocabulary_hint: str = "",
) -> str:
    """Build the full prompt for one video's candidate windows.

    ``windows`` are ``candidates.CandidateWindow`` — passed as the full
    object rather than raw text so each candidate's exact start/end is
    visible to the model, which is what lets it return THOSE timestamps
    back rather than ones it invents.
    """
    parts = [
        "You are selecting the strongest short-form clip candidates from a "
        "documentary or educational narration transcript, for vertical "
        "short-form video.",
    ]
    if source_title:
        parts.append("Source video title: %s" % source_title)
    if vocabulary_hint:
        parts.append("Names/terms that may appear: %s" % vocabulary_hint)

    parts.append(SCORING_RUBRIC)
    parts.append(CLIP_PRIORITY)
    parts.append(
        "Below are %d candidate spans, each already a self-contained block "
        "of consecutive transcript segments with its own start/end time. "
        "Score EVERY candidate listed — do not invent new spans, do not "
        "merge or split the ones given. Return the SAME start/end values "
        "you were given, unchanged." % len(windows)
    )

    for index, window in enumerate(windows):
        parts.append(
            "\nCandidate %d  [%.1f-%.1f]  (%.0fs)\n%s"
            % (index, window.start, window.end, window.duration,
               _format_segments(window.segments))
        )

    return "\n\n".join(parts)
