"""Title/description/hashtags (spec section 18).

Two constraints the spec states explicitly, and this module exists to make
true by construction rather than by review:

  * no keyword stuffing — hashtags are capped at a small fixed count, never
    derived from how many plausible tags the text could support.
  * no implying official F1 content — every description carries an explicit
    non-affiliation line, and the fixed hashtag base avoids anything that
    reads as an official account or property (no "F1", bare "Formula1" team
    hashtags, etc. beyond the generic fan-content ones below).
"""

from __future__ import annotations

from typing import List

from .types import RenderMetadata

BASE_HASHTAGS = ("F1", "Formula1", "Motorsport", "Shorts")

DISCLAIMER = (
    "Fan-made highlight clip. Not affiliated with, endorsed by, or "
    "sponsored by Formula 1, the FIA, or any team."
)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    # Cut at the last whitespace before the limit, not mid-word.
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return (cut or text[: limit - 1]).rstrip(",.;: ") + "…"


def build_title(hook: str, max_chars: int = 95) -> str:
    hook = (hook or "").strip()
    if not hook:
        hook = "F1 highlight clip"
    return _truncate(hook, max_chars)


def build_description(
    source_title: str, source_url: str, reason: str, transcript_text: str,
) -> str:
    snippet = _truncate(transcript_text or "", 200)
    parts = [DISCLAIMER, ""]
    if reason:
        parts.append(reason.strip())
    if snippet:
        parts.append('"%s"' % snippet)
    parts.append("")
    parts.append("Source: %s" % (source_title or "").strip())
    if source_url:
        parts.append(source_url)
    return "\n".join(p for p in parts if p is not None)


def build_hashtags(vocabulary_hint: str, max_hashtags: int = 6) -> List[str]:
    """The fixed base set, plus at most a couple of driver/team names from
    ``vocabulary_hint`` (the same config value transcription/analysis use to
    help the models recognise proper nouns) — never more than
    ``max_hashtags`` in total, so the cap is the actual limiter, not the
    number of names configured.
    """
    tags = list(BASE_HASHTAGS)
    for name in (vocabulary_hint or "").split(","):
        name = name.strip()
        if not name or not name.replace(" ", "").isalnum():
            continue
        tag = name.replace(" ", "")
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= max_hashtags:
            break
    return tags[:max_hashtags]


def build_metadata(
    hook: str, reason: str, transcript_text: str, source_title: str,
    source_url: str, vocabulary_hint: str = "",
    max_title_chars: int = 95, max_hashtags: int = 6,
) -> RenderMetadata:
    return RenderMetadata(
        title=build_title(hook, max_title_chars),
        description=build_description(source_title, source_url, reason, transcript_text),
        hashtags=build_hashtags(vocabulary_hint, max_hashtags),
    )
