"""Liquidity sweep detection — deterministic, pure-pandas.

Definition
----------
A **bearish sweep** at bar index ``sweep_bar`` (the sweep candle) occurs when:
    1. A recent confirmed swing high exists at price ``level``.
    2. bars[sweep_bar].high > level  (price trades *above* the swing high)
    3. bars[sweep_bar].close < level  (closes *back below* it in the same candle)
       OR bars[sweep_bar+1].close < level within ``max_close_bars`` bars.
    4. The penetration above level is >= ``min_sweep_distance``.

A **bullish sweep** is the symmetric definition using swing lows:
    1. A recent confirmed swing low exists at price ``level``.
    2. bars[sweep_bar].low < level
    3. bars[sweep_bar].close > level  (or within max_close_bars bars)
    4. Penetration >= min_sweep_distance.

Proxy assumptions (documented)
-------------------------------
- "Closes back" means the close of the sweep candle itself or the
  immediately following candle is on the rejecting side.  This is more
  forgiving than requiring a same-candle rejection — on a 5m bar the body
  sometimes straddles the level.
- We look at the *most recent* confirmed swing within ``lookback`` bars of
  current price to limit search scope.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift.models import Bar
from drift.strategy.primitives.swings import SwingPoint, find_swing_highs, find_swing_lows


@dataclass(frozen=True)
class SweepResult:
    """Description of a detected liquidity sweep."""

    kind: str               # "bearish" | "bullish"
    level: float            # the swept swing level
    sweep_bar_index: int    # bar where price traded through the level
    penetration: float      # how far price traded beyond the level
    rejection_bar_index: int  # bar where close confirmed rejection


def detect_bearish_sweep(
    bars: list[Bar],
    swing_lookback: int = 3,
    min_sweep_distance: float = 0.05,
    max_close_bars: int = 2,
    max_swing_age_bars: int = 40,
) -> SweepResult | None:
    """Scan for the most recent bearish liquidity sweep.

    Bearish sweep: price spikes above a prior swing high then closes back below.

    Args:
        bars:               OHLCV bars, oldest-first.
        swing_lookback:     Passed to find_swing_highs() for left/right confirmation.
        min_sweep_distance: Minimum points that price must trade beyond the level.
        max_close_bars:     Maximum bars after the spike to confirm rejection close.
        max_swing_age_bars: Only consider swing highs within this many bars of the
                            end of the bars list (limits search to recent structure).

    Returns:
        The most recent SweepResult, or None if no sweep is found.
    """
    if len(bars) < swing_lookback * 2 + 3:
        return None

    swing_highs = find_swing_highs(bars, lookback=swing_lookback)
    if not swing_highs:
        return None

    # Only consider recent swing highs
    cutoff = len(bars) - max_swing_age_bars
    recent_swings = [s for s in swing_highs if s.bar_index >= cutoff]
    if not recent_swings:
        recent_swings = swing_highs[-3:]  # fallback: last 3 swings

    # Search for a sweep from most recent bar backwards
    n = len(bars)
    best: SweepResult | None = None

    for swing in reversed(recent_swings):
        level = swing.price
        # Look for a bar after the swing that spikes above it
        for i in range(swing.bar_index + 1, n):
            if bars[i].high <= level + min_sweep_distance:
                continue  # didn't trade far enough above
            penetration = bars[i].high - level
            # Now look for a close back below the level within max_close_bars
            for j in range(i, min(i + max_close_bars + 1, n)):
                if bars[j].close < level:
                    best = SweepResult(
                        kind="bearish",
                        level=level,
                        sweep_bar_index=i,
                        penetration=penetration,
                        rejection_bar_index=j,
                    )
                    break
            if best is not None:
                break
        if best is not None:
            break

    return best


def detect_bullish_sweep(
    bars: list[Bar],
    swing_lookback: int = 3,
    min_sweep_distance: float = 0.05,
    max_close_bars: int = 2,
    max_swing_age_bars: int = 40,
) -> SweepResult | None:
    """Scan for the most recent bullish liquidity sweep.

    Bullish sweep: price spikes below a prior swing low then closes back above.

    Args:
        bars:               OHLCV bars, oldest-first.
        swing_lookback:     Passed to find_swing_lows() for left/right confirmation.
        min_sweep_distance: Minimum points that price must trade beyond the level.
        max_close_bars:     Maximum bars after the spike to confirm rejection close.
        max_swing_age_bars: Only consider swing lows within this many bars of end.

    Returns:
        The most recent SweepResult, or None if no sweep is found.
    """
    if len(bars) < swing_lookback * 2 + 3:
        return None

    swing_lows = find_swing_lows(bars, lookback=swing_lookback)
    if not swing_lows:
        return None

    cutoff = len(bars) - max_swing_age_bars
    recent_swings = [s for s in swing_lows if s.bar_index >= cutoff]
    if not recent_swings:
        recent_swings = swing_lows[-3:]

    n = len(bars)
    best: SweepResult | None = None

    for swing in reversed(recent_swings):
        level = swing.price
        for i in range(swing.bar_index + 1, n):
            if bars[i].low >= level - min_sweep_distance:
                continue
            penetration = level - bars[i].low
            for j in range(i, min(i + max_close_bars + 1, n)):
                if bars[j].close > level:
                    best = SweepResult(
                        kind="bullish",
                        level=level,
                        sweep_bar_index=i,
                        penetration=penetration,
                        rejection_bar_index=j,
                    )
                    break
            if best is not None:
                break
        if best is not None:
            break

    return best
