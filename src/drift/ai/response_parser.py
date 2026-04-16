from __future__ import annotations

import json
import logging
import re

from drift.models import LLMDecision

logger = logging.getLogger(__name__)

_NO_TRADE_FALLBACK = LLMDecision(
    decision="NO_TRADE",
    confidence=0,
    setup_type="parser_error",
    thesis="LLM response could not be parsed. Defaulting to NO_TRADE for safety.",
    entry_style="no_entry",
    entry_zone=[0.0, 0.0],
    invalidation_hint="n/a",
    hold_minutes=1,
    do_not_trade_if=["Parser failed — do not trade this cycle."],
)


class ResponseParser:
    """Parses a raw LLM text response into a validated LLMDecision.

    On any parse or validation error, logs a warning and returns the
    NO_TRADE fallback so the system always has a safe default.
    """

    def parse(self, raw_text: str) -> tuple[LLMDecision, dict]:
        """Parse the raw response text.

        Returns:
            (decision, raw_dict) where raw_dict is the deserialized JSON
            (or empty dict on failure) for logging purposes.
        """
        raw_dict: dict = {}
        try:
            raw_dict = self._extract_json(raw_text)
            # Claude sometimes returns hold_minutes=0 on NO_TRADE — clamp to 1
            # to satisfy the model constraint (hold_minutes is irrelevant for NO_TRADE anyway)
            if raw_dict.get("hold_minutes", 1) < 1:
                raw_dict["hold_minutes"] = 1
            decision = LLMDecision.model_validate(raw_dict)
            return decision, raw_dict
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM response parse failed: %s | raw=%r", exc, raw_text[:500])
            return _NO_TRADE_FALLBACK, raw_dict

    def _extract_json(self, text: str) -> dict:
        """Extract a JSON object from raw text, tolerating markdown fences
        and reasoning preamble before the JSON block."""
        # Try to find a fenced JSON block anywhere in the text (not just at the start)
        fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
        if fence_match:
            candidate = fence_match.group(1).strip()
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start != -1 and end > start:
                return json.loads(candidate[start : end + 1])

        # Fall back to finding the outermost JSON object in the raw text
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("No JSON object found in response")

        return json.loads(text[start : end + 1])
