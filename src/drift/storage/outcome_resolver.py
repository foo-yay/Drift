"""Live outcome resolver — automatically resolves TRADE_PLAN_ISSUED signals.

When ``run()`` is called (e.g. at the start of each scheduler cycle), it:

1. Loads all unresolved live ``TRADE_PLAN_ISSUED`` signals from SignalStore.
2. For each, fetches 1-minute bars from the signal's issue time to now using
   the YFinance provider.
3. Passes those bars through the same ``resolve_outcome()`` logic used by the
   replay engine.
4. Writes the result back to SQLite via ``SignalStore.upsert_outcome()``.

This means the performance context (which reads resolved outcomes) starts
accumulating real win/loss data automatically without manual replay runs.

Safe degradation: any individual resolution failure is logged and skipped so
one bad signal cannot block the others.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from math import ceil

from drift.models import Bar, TradePlan
from drift.replay.outcome import resolve_outcome
from drift.storage.signal_store import SignalRow, SignalStore

log = logging.getLogger(__name__)

# How many extra 1m bars to fetch beyond max_hold_minutes as a safety buffer.
_BAR_FETCH_BUFFER = 15


def resolve_live_outcomes(store: SignalStore, symbol: str, provider) -> int:  # type: ignore[type-arg]
    """Resolve all pending live signals for *symbol*.

    Args:
        store:    Open SignalStore (same db the scheduler writes to).
        symbol:   Instrument symbol (e.g. ``"MNQ"``).
        provider: A ``MarketDataProvider`` instance used to fetch 1m bars.

    Returns:
        Number of signals successfully resolved in this call.
    """
    pending = store.get_pending_live_signals(symbol)
    if not pending:
        return 0

    log.info("Outcome resolver: %d pending live signal(s) to resolve", len(pending))
    resolved = 0

    for row in pending:
        try:
            count = _resolve_one(store, row, provider)
            resolved += count
        except Exception as exc:  # noqa: BLE001
            log.warning("Outcome resolver: failed to resolve %s — %s", row.signal_key, exc)

    return resolved


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_one(store: SignalStore, row: SignalRow, provider) -> int:  # type: ignore[type-arg]
    """Attempt to resolve a single pending signal.  Returns 1 if resolved, 0 otherwise."""
    plan = _build_plan(row)
    if plan is None:
        log.debug("Outcome resolver: skipping %s — cannot reconstruct TradePlan", row.signal_key)
        return 0

    signal_time = datetime.fromisoformat(row.event_time_utc).replace(tzinfo=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    elapsed_minutes = (now - signal_time).total_seconds() / 60

    # Signal is still within its hold window — don't attempt to resolve yet.
    if elapsed_minutes < plan.max_hold_minutes:
        log.debug(
            "Outcome resolver: signal %s still within hold window (%.1f / %d min elapsed)",
            row.signal_key, elapsed_minutes, plan.max_hold_minutes,
        )
        return 0

    # Calculate how many 1m bars we need: from signal_time to now, capped
    # at max_hold_minutes + buffer.
    bars_needed = min(ceil(elapsed_minutes) + _BAR_FETCH_BUFFER, plan.max_hold_minutes + _BAR_FETCH_BUFFER)
    all_bars: list[Bar] = provider.get_recent_bars(symbol=row.symbol, timeframe="1m", lookback=bars_needed)

    # Filter to bars strictly after the signal was issued.
    bars_after = [b for b in all_bars if b.timestamp > signal_time]
    if not bars_after:
        log.debug("Outcome resolver: no 1m bars after signal time for %s", row.signal_key)
        return 0

    result = resolve_outcome(plan, bars_after)
    store.upsert_outcome(row.signal_key, result.outcome, result.pnl_points)
    log.info(
        "Outcome resolver: resolved %s → %s (%.1f pts, %d min)",
        row.signal_key, result.outcome, result.pnl_points, result.minutes_elapsed,
    )
    return 1


def _build_plan(row: SignalRow) -> TradePlan | None:
    """Reconstruct a TradePlan from the stored signal row.

    Returns None if required fields are missing (e.g. BLOCKED signals that
    never reached the planning stage).
    """
    required = (row.bias, row.stop_loss, row.take_profit_1, row.entry_min, row.entry_max)
    if any(v is None for v in required):
        return None

    # Retrieve max_hold_minutes from the stored trade plan JSON; fall back to
    # a conservative 60-minute default if not present.
    llm = row.llm_decision or {}
    max_hold = int(llm.get("hold_minutes", 60))
    max_hold = max(1, min(max_hold, 120))

    try:
        return TradePlan(
            generated_at=datetime.fromisoformat(row.event_time_utc),
            symbol=row.symbol,
            bias=row.bias,  # type: ignore[arg-type]
            setup_type=row.setup_type or "unknown",
            confidence=row.confidence or 0,
            entry_min=row.entry_min,
            entry_max=row.entry_max,
            stop_loss=row.stop_loss,
            take_profit_1=row.take_profit_1,
            take_profit_2=row.take_profit_2,
            reward_risk_ratio=row.reward_risk or 1.0,
            max_hold_minutes=max_hold,
            thesis=row.thesis or "",
            invalidation_conditions=[],
            operator_instructions=[],
            do_not_trade_if=[],
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("Outcome resolver: TradePlan reconstruction failed — %s", exc)
        return None
