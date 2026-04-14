"""Outcome resolver — evaluates trade plan hits and misses against subsequent bars.

After replay fires a signal, this module scans the 1m bars that follow and
determines whether the trade reached TP1, TP2, or was stopped out / time-stopped.

Logic per bar (evaluated in order, stop checked first to be conservative):

    LONG
        low  <= stop_loss   → STOP_HIT
        high >= tp2         → TP2_HIT  (if TP2 set)
        high >= tp1         → TP1_HIT

    SHORT
        high >= stop_loss   → STOP_HIT
        low  <= tp2         → TP2_HIT  (if TP2 set)
        low  <= tp1         → TP1_HIT

Same-bar ambiguity (stop and a target both touchable) is resolved conservatively
— STOP_HIT takes priority so the stats are not inflated.

If max hold time expires without resolution: TIME_STOP.
If bars run out before max hold time (session ended): SESSION_END.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from drift.models import Bar, TradePlan

OutcomeLabel = Literal["TP1_HIT", "TP2_HIT", "STOP_HIT", "TIME_STOP", "SESSION_END"]


@dataclass
class OutcomeResult:
    outcome: OutcomeLabel
    bars_elapsed: int
    minutes_elapsed: int
    exit_price: float
    pnl_points: float  # positive = profit, negative = loss relative to entry midpoint


def resolve_outcome(plan: TradePlan, bars_after: list[Bar]) -> OutcomeResult:
    """Scan bars following a signal and return the first resolved outcome.

    Args:
        plan:       The issued TradePlan (contains stop, targets, hold limit, bias).
        bars_after: 1m bars strictly after the signal bar, in chronological order.

    Returns:
        An OutcomeResult describing how the trade resolved.
    """
    entry = (plan.entry_min + plan.entry_max) / 2
    is_long = plan.bias == "LONG"
    max_bars = plan.max_hold_minutes  # 1m bars == 1 minute each

    for i, bar in enumerate(bars_after[:max_bars]):
        minutes = i + 1

        if is_long:
            # Check stop first — conservative same-bar tie-break
            if bar.low <= plan.stop_loss:
                return OutcomeResult(
                    outcome="STOP_HIT",
                    bars_elapsed=minutes,
                    minutes_elapsed=minutes,
                    exit_price=plan.stop_loss,
                    pnl_points=plan.stop_loss - entry,
                )
            if plan.take_profit_2 is not None and bar.high >= plan.take_profit_2:
                return OutcomeResult(
                    outcome="TP2_HIT",
                    bars_elapsed=minutes,
                    minutes_elapsed=minutes,
                    exit_price=plan.take_profit_2,
                    pnl_points=plan.take_profit_2 - entry,
                )
            if bar.high >= plan.take_profit_1:
                return OutcomeResult(
                    outcome="TP1_HIT",
                    bars_elapsed=minutes,
                    minutes_elapsed=minutes,
                    exit_price=plan.take_profit_1,
                    pnl_points=plan.take_profit_1 - entry,
                )

        else:  # SHORT
            if bar.high >= plan.stop_loss:
                return OutcomeResult(
                    outcome="STOP_HIT",
                    bars_elapsed=minutes,
                    minutes_elapsed=minutes,
                    exit_price=plan.stop_loss,
                    pnl_points=entry - plan.stop_loss,
                )
            if plan.take_profit_2 is not None and bar.low <= plan.take_profit_2:
                return OutcomeResult(
                    outcome="TP2_HIT",
                    bars_elapsed=minutes,
                    minutes_elapsed=minutes,
                    exit_price=plan.take_profit_2,
                    pnl_points=entry - plan.take_profit_2,
                )
            if bar.low <= plan.take_profit_1:
                return OutcomeResult(
                    outcome="TP1_HIT",
                    bars_elapsed=minutes,
                    minutes_elapsed=minutes,
                    exit_price=plan.take_profit_1,
                    pnl_points=entry - plan.take_profit_1,
                )

    # Ran out of bars before max hold — session ended
    if len(bars_after) < max_bars:
        exit_price = bars_after[-1].close if bars_after else entry
        pnl = (exit_price - entry) if is_long else (entry - exit_price)
        return OutcomeResult(
            outcome="SESSION_END",
            bars_elapsed=len(bars_after),
            minutes_elapsed=len(bars_after),
            exit_price=exit_price,
            pnl_points=pnl,
        )

    # Time stop — max hold elapsed, exit at last bar's close
    exit_price = bars_after[max_bars - 1].close
    pnl = (exit_price - entry) if is_long else (entry - exit_price)
    return OutcomeResult(
        outcome="TIME_STOP",
        bars_elapsed=max_bars,
        minutes_elapsed=max_bars,
        exit_price=exit_price,
        pnl_points=pnl,
    )
