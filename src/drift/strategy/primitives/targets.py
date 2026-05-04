"""External liquidity target detection — deterministic.

Definition
----------
"External liquidity" is the nearest obvious price level that opposing
participants have resting orders at (stop-losses / limit orders).

Proxy assumptions (documented)
-------------------------------
We use swing extremes as proxies for external liquidity pools:
    - For LONG:  prior swing highs above current price, nearest first
    - For SHORT: prior swing lows below current price, nearest first

Session high/low are included as fallback targets when no swing level is
available in the right direction.

This is a reasonable mechanical proxy.  In practice, liquidity pools form
at obvious round numbers, prior day highs/lows, and equal highs/lows.  The
swing-based approach captures the most common manifestation without
requiring order book data.

Two targets are returned:
    tp1 — nearest qualifying level (primary target, higher-probability)
    tp2 — second nearest level (extended target, optional scale-out)

Minimum distance filtering: targets must be at least min_target_distance
points from the entry zone to avoid trivially close levels.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift.models import Bar
from drift.strategy.primitives.swings import find_swing_highs, find_swing_lows


@dataclass(frozen=True)
class LiquidityTarget:
    price: float
    source: str         # "swing_high" | "swing_low" | "session_high" | "session_low"
    bar_index: int      # bar where the source level was established (-1 for session)


def find_long_targets(
    bars: list[Bar],
    entry_max: float,
    min_target_distance: float = 0.10,
    swing_lookback: int = 3,
    max_swing_age_bars: int = 60,
    n_targets: int = 2,
) -> list[LiquidityTarget]:
    """Return up to n_targets liquidity levels above entry_max, nearest first.

    For a LONG setup: targets are prior swing highs above entry.

    Args:
        bars:                 Full bars list, oldest-first.
        entry_max:            Worst-case LONG fill price.
        min_target_distance:  Target must be this many points above entry_max.
        swing_lookback:       Swing detection lookback.
        max_swing_age_bars:   Only use swing highs within this many bars of end.
        n_targets:            Maximum number of targets to return.

    Returns:
        List of LiquidityTarget objects sorted by price ascending (nearest first).
    """
    swing_highs = find_swing_highs(bars, lookback=swing_lookback)
    cutoff = max(0, len(bars) - max_swing_age_bars)
    recent = [s for s in swing_highs if s.bar_index >= cutoff]

    candidates: list[LiquidityTarget] = []
    seen: set[float] = set()

    for swing in recent:
        if swing.price > entry_max + min_target_distance and swing.price not in seen:
            seen.add(swing.price)
            candidates.append(LiquidityTarget(
                price=swing.price,
                source="swing_high",
                bar_index=swing.bar_index,
            ))

    # Fallback: session high
    session_high = max(b.high for b in bars)
    if session_high > entry_max + min_target_distance and session_high not in seen:
        candidates.append(LiquidityTarget(
            price=session_high,
            source="session_high",
            bar_index=-1,
        ))

    # Sort by price ascending (nearest above entry first)
    candidates.sort(key=lambda t: t.price)

    # Deduplicate near-equal levels (within 0.01 * entry_max — 1 penny per $100)
    deduped: list[LiquidityTarget] = []
    for c in candidates:
        if deduped and abs(c.price - deduped[-1].price) < entry_max * 0.0001:
            continue
        deduped.append(c)

    return deduped[:n_targets]


def find_short_targets(
    bars: list[Bar],
    entry_min: float,
    min_target_distance: float = 0.10,
    swing_lookback: int = 3,
    max_swing_age_bars: int = 60,
    n_targets: int = 2,
) -> list[LiquidityTarget]:
    """Return up to n_targets liquidity levels below entry_min, nearest first.

    For a SHORT setup: targets are prior swing lows below entry.

    Args:
        bars:                 Full bars list, oldest-first.
        entry_min:            Worst-case SHORT fill price.
        min_target_distance:  Target must be this many points below entry_min.
        swing_lookback:       Swing detection lookback.
        max_swing_age_bars:   Only use swing lows within this many bars of end.
        n_targets:            Maximum number of targets to return.

    Returns:
        List of LiquidityTarget objects sorted by price descending (nearest first).
    """
    swing_lows = find_swing_lows(bars, lookback=swing_lookback)
    cutoff = max(0, len(bars) - max_swing_age_bars)
    recent = [s for s in swing_lows if s.bar_index >= cutoff]

    candidates: list[LiquidityTarget] = []
    seen: set[float] = set()

    for swing in recent:
        if swing.price < entry_min - min_target_distance and swing.price not in seen:
            seen.add(swing.price)
            candidates.append(LiquidityTarget(
                price=swing.price,
                source="swing_low",
                bar_index=swing.bar_index,
            ))

    session_low = min(b.low for b in bars)
    if session_low < entry_min - min_target_distance and session_low not in seen:
        candidates.append(LiquidityTarget(
            price=session_low,
            source="session_low",
            bar_index=-1,
        ))

    # Sort by price descending (nearest below entry first)
    candidates.sort(key=lambda t: t.price, reverse=True)

    deduped: list[LiquidityTarget] = []
    for c in candidates:
        if deduped and abs(c.price - deduped[-1].price) < entry_min * 0.0001:
            continue
        deduped.append(c)

    return deduped[:n_targets]
