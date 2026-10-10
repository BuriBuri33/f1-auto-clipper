"""LLM-based clip selection — the Gemini backend.

Calls the free-tier Gemini API with structured output (``responseSchema``)
and validates the response strictly: a candidate entry with an out-of-range
score, a timestamp that does not match one we sent, or a missing field is
REJECTED, never coerced into something plausible-looking. Spec section 10
says "Output strict JSON"; silently clamping a score or inventing a missing
field would defeat the purpose of asking for structured output at all.

Endpoint, error shapes and the thinking-budget behaviour below were verified
against the live API on 2026-10-10 — see ``prompts.py`` and
``docs/LIMITS.md`` section 5 for the measurements.
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional

import requests

from ..transcription.types import Segment
from .candidates import CandidateWindow
from .prompts import RESPONSE_SCHEMA, build_prompt
from .types import AnalysisError, AnalysisQuotaExceeded, ClipCandidate, SubScores

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# An alias, not a pinned snapshot. Verified 2026-10-10: the previously
# documented default, gemini-2.5-flash, now 404s with "no longer available
# to new users" — a pinned model name rots. gemini-flash-latest tracks
# whatever Google currently calls its stable flash tier, trading a small
# amount of behaviour drift for never going dead on its own.
DEFAULT_MODEL = "gemini-flash-latest"

# A window that does not match one of the candidates we sent is rejected
# rather than accepted at face value — this is the tolerance for float
# round-tripping through JSON, not a tolerance for the model inventing times.
TIMESTAMP_MATCH_TOLERANCE = 0.5


class GeminiClipSelector:
    """Scores candidate windows with the Gemini free tier."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        thinking_budget: int = 0,
        timeout: int = 60,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        session=None,
        sleep=time.sleep,
    ) -> None:
        if not api_key:
            raise AnalysisError("no Gemini API key supplied")
        self.api_key = api_key
        self.model = model
        # 0 is a deliberate choice, not the API default: verified that with
        # responseSchema set, the model's OWN default thinking budget spent
        # 283-716 tokens and 12-40s "thinking" about a four-segment
        # transcript with no measured quality improvement over budget=0.
        self.thinking_budget = thinking_budget
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.session = session or requests
        self.sleep = sleep

    def score_windows(
        self,
        windows: List[CandidateWindow],
        source_title: str = "",
        vocabulary_hint: str = "",
    ) -> List[ClipCandidate]:
        """Score a batch of candidate windows. Returns validated candidates only.

        A window the model fails to return, or returns malformed, is simply
        absent from the result — the caller treats a shrunk result as
        partial success, not as a reason to fail the whole batch, since one
        bad entry among many must not discard the rest.
        """
        if not windows:
            return []

        prompt = build_prompt(windows, source_title=source_title,
                              vocabulary_hint=vocabulary_hint)
        payload = self._call(prompt)
        raw_clips = payload.get("clips")
        if not isinstance(raw_clips, list):
            raise AnalysisError(
                "Gemini response has no 'clips' list — got keys: %s"
                % sorted(payload) if isinstance(payload, dict) else type(payload)
            )

        candidates: List[ClipCandidate] = []
        for entry in raw_clips:
            candidate = self._validate_entry(entry, windows)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _validate_entry(
        self, entry: Any, windows: List[CandidateWindow]
    ) -> Optional[ClipCandidate]:
        """Strict validation: reject, never coerce. Returns None on rejection."""
        if not isinstance(entry, dict):
            return None

        try:
            start = float(entry["start"])
            end = float(entry["end"])
            sub_scores = SubScores(
                hook=int(entry["hook_score"]),
                emotional=int(entry["emotional_score"]),
                context=int(entry["context_score"]),
                payoff=int(entry["payoff_score"]),
                shareability=int(entry["shareability_score"]),
            )
            reason = str(entry["reason"]).strip()
            hook = str(entry["hook"]).strip()
        except (KeyError, TypeError, ValueError):
            # Missing field, wrong type, or a sub-score outside 0-100
            # (SubScores.__post_init__ raises ValueError for that).
            return None

        if end <= start or not reason or not hook:
            return None

        # The returned span must match one of the windows we actually sent —
        # not approximately plausible, but the SAME span, within float
        # round-tripping tolerance. This is what "return strict JSON" buys:
        # a model that invents a new span is caught here, not trusted.
        match = next(
            (w for w in windows
             if abs(w.start - start) <= TIMESTAMP_MATCH_TOLERANCE
             and abs(w.end - end) <= TIMESTAMP_MATCH_TOLERANCE),
            None,
        )
        if match is None:
            return None

        return ClipCandidate(
            start=match.start, end=match.end, sub_scores=sub_scores,
            reason=reason, hook=hook, text=match.text,
            method="llm", model=self.model,
        )

    def _call(self, prompt: str) -> Dict[str, Any]:
        url = "%s/%s:generateContent" % (API_BASE, self.model)
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "temperature": 0.2,
                "thinkingConfig": {"thinkingBudget": self.thinking_budget},
            },
        }

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(
                    url, params={"key": self.api_key}, json=body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = AnalysisError("Gemini request failed: %s" % exc)
            else:
                if response.status_code == 200:
                    return self._parse(response)

                error_body = self._error_body(response)
                status = (error_body.get("error") or {}).get("status", "")
                reason = self._error_reason(error_body)

                if reason == "API_KEY_INVALID":
                    raise AnalysisError(
                        "Gemini rejected the key. Check LLM_API_KEY; get a "
                        "free one at https://aistudio.google.com/apikey"
                    )
                if status == "RESOURCE_EXHAUSTED":
                    # NOTE: this status string is Google's documented
                    # convention for quota errors; not triggered against the
                    # live API during development, to avoid spending the
                    # project's own free allowance proving it. Verify if a
                    # real exhaustion is ever observed and this does not
                    # match.
                    raise AnalysisQuotaExceeded(
                        "Gemini's free-tier allowance is spent. The video "
                        "stays queued; nothing is lost and nothing is charged."
                    )
                if response.status_code == 429:
                    last_error = AnalysisError("Gemini rate limited (429)")
                elif response.status_code >= 500:
                    last_error = AnalysisError(
                        "Gemini returned HTTP %d" % response.status_code
                    )
                elif response.status_code == 400:
                    raise AnalysisError(
                        "Gemini rejected the request: %s"
                        % (error_body.get("error") or {}).get("message", "bad request")
                    )
                else:
                    raise AnalysisError(
                        "Gemini returned HTTP %d: %s"
                        % (response.status_code, str(error_body)[:300])
                    )

            if attempt < self.max_retries - 1:
                delay = self.backoff_base * (2 ** attempt)
                self.sleep(delay + random.uniform(0, delay * 0.25))

        raise last_error or AnalysisError("Gemini could not be reached")

    @staticmethod
    def _error_body(response) -> Dict[str, Any]:
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _error_reason(error_body: Dict[str, Any]) -> str:
        for detail in (error_body.get("error") or {}).get("details", []):
            if isinstance(detail, dict) and "reason" in detail:
                return detail["reason"]
        return ""

    @staticmethod
    def _parse(response) -> Dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise AnalysisError("Gemini returned non-JSON: %s" % exc) from exc
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AnalysisError(
                "Gemini response has no text part — got: %s" % str(data)[:300]
            ) from exc
        import json
        try:
            return json.loads(text)
        except ValueError as exc:
            raise AnalysisError(
                "Gemini's text was not valid JSON: %s" % str(text)[:300]
            ) from exc
