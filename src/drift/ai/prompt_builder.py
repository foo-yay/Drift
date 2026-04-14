from __future__ import annotations

import json

from drift.models import GateReport, MarketSnapshot


_SYSTEM_PROMPT = """\
You are a disciplined futures trade adjudication engine for MNQ (Micro Nasdaq futures).

Your task is to evaluate a structured market snapshot and return a single JSON object.
You must return one of: LONG, SHORT, or NO_TRADE.

Rules:
- You are NOT allowed to invent data. Base all reasoning on the provided snapshot only.
- Favor NO_TRADE when the setup is unclear, extended, conflicting, or has poor reward-to-risk.
- Prefer continuation entries when trend and momentum align and extension risk is moderate.
- Prefer failed_breakout_reversion only when rejection and structure are clear.
- setup_type MUST be exactly one of: pullback_continuation, breakout_continuation, failed_breakout_reversion, no_trade. Do not invent other values.
- Reject low-quality chop, late entries, and setups with ambiguous invalidation.
- Be selective. Most cycles should return NO_TRADE.

You must return valid JSON matching this exact schema. No markdown, no extra text:

{
  "decision": "LONG" | "SHORT" | "NO_TRADE",
  "confidence": <integer 0-100>,
  "setup_type": "pullback_continuation" | "breakout_continuation" | "failed_breakout_reversion" | "no_trade",
  "thesis": "<string>",
  "entry_style": "buy_pullback" | "buy_breakout" | "sell_pullback" | "sell_breakout" | "no_entry",
  "entry_zone": [<low_price>, <high_price>],
  "invalidation_hint": "<string>",
  "hold_minutes": <integer 1-120>,
  "do_not_trade_if": ["<condition>", ...]
}

If decision is NO_TRADE, set setup_type to "no_trade", entry_zone to [0.0, 0.0], and hold_minutes to 1.
"""


class PromptBuilder:
    """Converts a MarketSnapshot and GateReport into a Claude API message list."""

    def build(self, snapshot: MarketSnapshot, gate_report: GateReport) -> list[dict]:
        """Return the messages list for the Anthropic messages API."""
        user_content = self._format_snapshot(snapshot, gate_report)
        return [{"role": "user", "content": user_content}]

    @property
    def system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _format_snapshot(self, snapshot: MarketSnapshot, gate_report: GateReport) -> str:
        scores = {
            "trend_score": snapshot.trend_score,
            "momentum_score": snapshot.momentum_score,
            "volatility_score": snapshot.volatility_score,
            "extension_risk": snapshot.extension_risk,
            "structure_quality": snapshot.structure_quality,
            "pullback_quality": snapshot.pullback_quality,
            "breakout_quality": snapshot.breakout_quality,
            "mean_reversion_risk": snapshot.mean_reversion_risk,
            "session_alignment": snapshot.session_alignment,
        }
        states = {
            "short_trend_state": snapshot.short_trend_state,
            "medium_trend_state": snapshot.medium_trend_state,
            "momentum_state": snapshot.momentum_state,
            "volatility_regime": snapshot.volatility_regime,
        }
        gate_summary = [
            {"gate": r.gate_name, "passed": r.passed, "reason": r.reason}
            for r in gate_report.results
        ]

        order_blocks = getattr(snapshot, "order_blocks", None)
        rejection_blocks = getattr(snapshot, "rejection_blocks", None)

        payload: dict = {
            "symbol": snapshot.symbol,
            "as_of_utc": snapshot.as_of.isoformat(),
            "last_price": snapshot.last_price,
            "session": snapshot.session,
            "bar_counts": {
                "1m": snapshot.bars_1m_count,
                "5m": snapshot.bars_5m_count,
                "1h": snapshot.bars_1h_count,
            },
            "regime_scores": scores,
            "market_states": states,
            "gate_results": gate_summary,
        }

        if order_blocks:
            payload["order_blocks"] = order_blocks

        if rejection_blocks:
            payload["rejection_blocks"] = rejection_blocks

        if snapshot.market_note:
            payload["market_note"] = snapshot.market_note

        return (
            "Evaluate the following market snapshot and return a JSON trading decision:\n\n"
            + json.dumps(payload, indent=2)
        )
