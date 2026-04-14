from __future__ import annotations

import json
import logging

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
            decision = LLMDecision.model_validate(raw_dict)
            return decision, raw_dict
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM response parse failed: %s | raw=%r", exc, raw_text[:500])
            return _NO_TRADE_FALLBACK, raw_dict

    def _extract_json(self, text: str) -> dict:
        """Extract a JSON object from raw text, tolerating markdown fences."""
        stripped = text.strip()

        # Strip markdown code fences if present
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            # Drop the opening fence line and trailing fence
            inner_lines = []
            in_fence = False
            for line in lines:
                if line.startswith("```") and not in_fence:
                    in_fence = True
                    continue
                if line.startswith("```") and in_fence:
                    break
                if in_fence:
                    inner_lines.append(line)
            stripped = "\n".join(inner_lines).strip()

        # Find the outermost JSON object
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("No JSON object found in response")

        return json.loads(stripped[start : end + 1])
