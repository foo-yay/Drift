"""Position advisor — structured LLM assessment for active positions.

Runs a separate LLM query that evaluates the current state of an open or
working position and returns a structured ``AssessmentRecommendation`` with
concrete parameter changes the operator can approve and apply.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from drift.models import AssessmentRecommendation

log = logging.getLogger(__name__)

# Fallback prompt used when prompts.yaml doesn't have assess_system_prompt
_DEFAULT_SYSTEM_PROMPT = """\
You are a trade position advisor for MNQ (Micro E-mini Nasdaq-100 Futures).
Evaluate the position and return a JSON object with keys:
action ("HOLD"|"ADJUST"|"CLOSE"), confidence (0-100), rationale (string),
new_stop_loss, new_take_profit_1, new_take_profit_2, new_entry_limit,
new_max_hold_minutes, recommended_exit_mode, risk_flags (list of strings).
Set unchanged fields to null.  Be direct.  No disclaimers.
"""

_HOLD_FALLBACK = AssessmentRecommendation(
    action="HOLD",
    confidence=0,
    rationale="Assessment unavailable — defaulting to HOLD.",
)


def _load_system_prompt(config: Any) -> str:
    """Load the assess system prompt from prompts.yaml, with fallback."""
    try:
        import yaml

        prompts_path = Path("config/prompts.yaml")
        if prompts_path.exists():
            data = yaml.safe_load(prompts_path.read_text())
            if data and "assess_system_prompt" in data:
                return data["assess_system_prompt"]
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_SYSTEM_PROMPT


def _extract_json(text: str) -> dict:
    """Extract JSON object from LLM response text."""
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            return json.loads(candidate[start : end + 1])

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ValueError("No JSON object found in LLM response")


def assess_position(config: Any, pos: Any) -> AssessmentRecommendation:
    """Run a structured LLM assessment for a position.

    Args:
        config: AppConfig
        pos: TradeRow (WORKING or FILLED)

    Returns:
        AssessmentRecommendation with action + recommended changes.
    """
    import anthropic

    api_key = os.environ.get(config.llm.api_key_env, "")
    if not api_key:
        return _HOLD_FALLBACK.model_copy(
            update={"rationale": "LLM API key not configured — cannot assess."},
        )

    # Fetch current price
    current_price = None
    try:
        from drift.data.providers.yfinance_provider import YFinanceProvider
        current_price = YFinanceProvider().get_latest_quote(pos.symbol)
    except Exception:  # noqa: BLE001
        pass

    # P&L calculation (FILLED only)
    pnl_pts = None
    entry_ref = pos.entry_fill or pos.entry_limit
    if entry_ref and current_price:
        pnl_pts = (current_price - entry_ref) if pos.bias == "LONG" else (entry_ref - current_price)

    payload = {
        "symbol": pos.symbol,
        "bias": pos.bias,
        "state": pos.state,
        "setup_type": pos.setup_type,
        "entry_fill": pos.entry_fill,
        "entry_limit": pos.entry_limit,
        "entry_zone": f"{pos.entry_min:.2f}–{pos.entry_max:.2f}",
        "current_price": current_price,
        "unrealized_pnl_points": round(pnl_pts, 2) if pnl_pts else None,
        "stop_loss": pos.stop_loss,
        "take_profit_1": pos.take_profit_1,
        "take_profit_2": pos.take_profit_2,
        "active_tp": pos.active_tp,
        "exit_mode": pos.exit_mode,
        "max_hold_minutes": pos.max_hold_minutes,
        "fill_time": pos.fill_time,
        "thesis": pos.thesis,
    }

    system_prompt = _load_system_prompt(config)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=512,
            temperature=0.3,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    "Assess this position and return your recommendation as JSON:\n\n"
                    + json.dumps(payload, indent=2)
                ),
            }],
            timeout=15,
        )
        raw_text = response.content[0].text
        raw_dict = _extract_json(raw_text)

        # Clamp confidence
        if raw_dict.get("confidence", 0) < 0:
            raw_dict["confidence"] = 0

        rec = AssessmentRecommendation.model_validate(raw_dict)
        log.info(
            "Assessment for trade %d: action=%s confidence=%d",
            pos.id, rec.action, rec.confidence,
        )
        return rec

    except Exception as exc:  # noqa: BLE001
        log.warning("Assess LLM call failed: %s", exc)
        return _HOLD_FALLBACK.model_copy(
            update={"rationale": f"Assessment failed: {exc}"},
        )
