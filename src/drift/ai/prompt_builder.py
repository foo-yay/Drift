from __future__ import annotations

import json
from typing import TYPE_CHECKING

from drift.models import GateReport, MarketSnapshot

if TYPE_CHECKING:
    from drift.scoring.performance_context import PerformanceContext


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

WATCH CONDITIONS (required on NO_TRADE):
When decision is NO_TRADE, you MUST populate watch_conditions with 1-3 specific,
actionable price or RSI levels that would make this setup tradeable. These are
monitored in real-time — when any condition is met, a new full cycle is triggered
automatically so the opportunity is never missed due to polling timing.

Each watch condition must specify:
- condition_type: exactly one of "price_above", "price_below", "rsi_above", "rsi_below"
- value: the exact numeric threshold (price in points, RSI as 0-100 integer)
- description: one clear sentence explaining what this level represents and why it matters
- expires_minutes: how long to watch for this condition (5-480 minutes)

Examples of good watch conditions:
  {"condition_type": "price_below", "value": 21000.0, "description": "Pullback to key support and VWAP confluence — would improve R:R significantly", "expires_minutes": 60}
  {"condition_type": "rsi_below", "value": 40, "description": "RSI reset from overbought would signal momentum exhaustion and potential reversal", "expires_minutes": 45}
  {"condition_type": "price_above", "value": 21150.0, "description": "Break above overnight high confirms breakout continuation setup", "expires_minutes": 120}

When decision is LONG or SHORT, set watch_conditions to [].

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
  "do_not_trade_if": ["<condition>", ...],
  "watch_conditions": [
    {
      "condition_type": "price_above" | "price_below" | "rsi_above" | "rsi_below",
      "value": <number>,
      "description": "<string>",
      "expires_minutes": <integer 5-480>
    }
  ]
}

If decision is NO_TRADE, set setup_type to "no_trade", entry_zone to [0.0, 0.0], hold_minutes to 1,
and populate watch_conditions with 1-3 specific triggers.
If decision is LONG or SHORT, set watch_conditions to [].
"""


class PromptBuilder:
    """Converts a MarketSnapshot and GateReport into a Claude API message list."""

    def __init__(self) -> None:
        self._perf_context: PerformanceContext | None = None

    def set_performance_context(self, ctx: PerformanceContext | None) -> None:
        """Update the performance context injected into the next system prompt build."""
        self._perf_context = ctx

    def build(self, snapshot: MarketSnapshot, gate_report: GateReport) -> list[dict]:
        """Return the messages list for the Anthropic messages API."""
        user_content = self._format_snapshot(snapshot, gate_report)
        return [{"role": "user", "content": user_content}]

    @property
    def system_prompt(self) -> str:
        if self._perf_context is None:
            return _SYSTEM_PROMPT
        return _SYSTEM_PROMPT + "\n\n" + _format_performance_block(self._perf_context)

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


# ---------------------------------------------------------------------------
# Performance context formatting
# ---------------------------------------------------------------------------


def _format_performance_block(ctx: PerformanceContext) -> str:
    """Render the PerformanceContext as a plain-text block to append to the system prompt."""
    from drift.scoring.performance_context import PerformanceContext  # local import avoids circular

    lines: list[str] = [
        "--- RECENT PERFORMANCE CONTEXT ---",
        f"Data window: last {ctx.lookback_days} days | Resolved signals: {ctx.resolved_count}",
        f"Overall win rate: {ctx.overall_win_rate_pct}%",
    ]

    # Streak
    if ctx.recent_streak > 0:
        lines.append(f"Recent streak: +{ctx.recent_streak} consecutive wins — avoid overconfidence.")
    elif ctx.recent_streak < 0:
        lines.append(f"Recent streak: {ctx.recent_streak} consecutive losses — apply extra scrutiny.")
    else:
        lines.append("Recent streak: mixed (no clear run).")

    # Hour of day guidance
    if ctx.best_hour_utc is not None:
        lines.append(
            f"Best hour (UTC): {ctx.best_hour_utc:02d}xx | "
            f"Weakest hour (UTC): {ctx.worst_hour_utc:02d}xx"
        )

    # Per-setup-type stats
    if ctx.setup_stats:
        lines.append("Per-setup performance:")
        for s in ctx.setup_stats:
            lines.append(
                f"  {s.setup_type}: {s.total} signals, "
                f"{s.win_rate_pct}% win rate, "
                f"avg pnl={s.avg_pnl_points:+.1f} pts"
            )

    # Few-shot examples
    if ctx.few_shot_examples:
        lines.append("Recent signal examples (use for calibration — do NOT copy the decision blindly):")
        for i, ex in enumerate(ctx.few_shot_examples, 1):
            lines.append(
                f"  [{i}] {ex.event_time_utc[:16]}Z | {ex.bias} {ex.setup_type} "
                f"conf={ex.confidence} → {ex.outcome} ({ex.pnl_points:+.1f} pts)"
            )
            if ex.thesis:
                thesis_short = ex.thesis[:200].replace("\n", " ")
                lines.append(f"      thesis: {thesis_short}")

    lines.append("--- END PERFORMANCE CONTEXT ---")
    return "\n".join(lines)
