"""Position advisor — on-demand LLM assessment for active positions.

Runs a separate, lightweight LLM query that evaluates the current state of an
open position (entry fill, current P&L, SL/TP levels, elapsed time) and
returns an advisory recommendation.  This does NOT affect the main signal
pipeline — it's a pure read-only assessment.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_ASSESS_SYSTEM_PROMPT = """\
You are a trade position advisor for MNQ (Micro E-mini Nasdaq-100 Futures).

You are evaluating an ACTIVE position.  The operator wants your assessment of
whether to hold, move the take-profit target, or close.  You are advisory only —
the operator makes the final decision.

Respond in 2-3 concise sentences.  Include:
1. Whether current conditions favor continuing to hold or closing
2. Whether the current exit mode (TP1/TP2/Manual) is appropriate
3. Any specific risk you see (e.g. approaching resistance, momentum fading)

Be direct and actionable.  No disclaimers, no hedging.
"""


def assess_position(config: Any, pos: Any) -> str:
    """Run a quick LLM assessment for an active position.

    Args:
        config: AppConfig
        pos: ActivePositionRow

    Returns:
        Advisory text from the LLM.
    """
    import anthropic

    api_key = os.environ.get(config.llm.api_key_env, "")
    if not api_key:
        return "LLM API key not configured — cannot assess."

    # Fetch current price for context
    current_price = None
    try:
        from drift.data.providers.yfinance_provider import YFinanceProvider
        current_price = YFinanceProvider().get_latest_quote(pos.symbol)
    except Exception:  # noqa: BLE001
        pass

    # Build the position context
    pnl_pts = None
    if pos.entry_fill and current_price:
        pnl_pts = (current_price - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current_price)

    payload = {
        "symbol": pos.symbol,
        "bias": pos.bias,
        "setup_type": pos.setup_type,
        "entry_fill": pos.entry_fill,
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

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=config.llm.model,
            max_tokens=256,
            temperature=0.3,
            system=_ASSESS_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "Assess this active position and advise:\n\n"
                    + json.dumps(payload, indent=2)
                ),
            }],
            timeout=15,
        )
        return response.content[0].text
    except Exception as exc:  # noqa: BLE001
        log.warning("Quick-assess LLM call failed: %s", exc)
        return f"Assessment unavailable: {exc}"
