"""Fair Value Gap (FVG) detection — deterministic, pure-pandas.

Definition
----------
A **bullish FVG** exists across bars at indices (i, i+1, i+2) when:
    bars[i+2].low > bars[i].high
    (gap between candle i's high and candle i+2's low is unfilled)

A **bearish FVG** exists across bars at indices (i, i+1, i+2) when:
    bars[i+2].high < bars[i].low
    (gap between candle i's low and candle i+2's high is unfilled)

The "imbalance" FVG represents a zone where price moved so rapidly in one
direction that the opposing side never had a chance to trade — a standard
3-candle imbalance, the same definition used universally in SMC/ICT literature.

Proxy assumptions (documented)
-------------------------------
- We look at the gap between bar[i].high and bar[i+2].low (bullish), not
  the displacement candle body.  This is the most common mechanical definition
  and avoids body-size subjectivity.
- min_gap_size is expressed in the same price units as the instrument.  For
  SPY at ~550 a gap of 0.10 is already meaningful; for MNQ at ~19000 you
  might want 5.0.  This is configurable.
- We scan the bars list and return all valid FVGs, oldest-first.
- A caller can then filter by recency, or check whether current price is
  "inside" a gap zone.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift.models import Bar


@dataclass(frozen=True)
class FVG:
    """A detected Fair Value Gap zone."""

    kind: str               # "bullish" | "bearish"
    top: float              # upper bound of the gap zone
    bottom: float           # lower bound of the gap zone
    mid: float              # midpoint (useful for limit entry targeting)
    gap_size: float         # top - bottom
    anchor_bar_index: int   # index of bar i (the first candle of the 3-bar pattern)
    displacement_bar_index: int  # index of bar i+1 (the displacement candle)

    def price_inside(self, price: float) -> bool:
        """Return True if price is within the FVG zone."""
        return self.bottom <= price <= self.top

    def price_tapped(self, bar: Bar) -> bool:
        """Return True if bar's range overlaps the FVG zone at all."""
        return bar.low <= self.top and bar.high >= self.bottom

    def price_fully_closed(self, bar: Bar) -> bool:
        """Return True if bar closed fully through (filled) the FVG zone."""
        if self.kind == "bullish":
            return bar.close <= self.bottom
        return bar.close >= self.top


def find_fvgs(
    bars: list[Bar],
    min_gap_size: float = 0.05,
    max_age_bars: int = 30,
) -> list[FVG]:
    """Scan bars for all valid FVGs within the lookback window.

    Args:
        bars:         OHLCV bars, oldest-first.
        min_gap_size: Minimum gap size to qualify.  Prevents micro-gaps from
                      triggering on illiquid instruments.
        max_age_bars: Only return FVGs whose anchor bar is within this many
                      bars of the end of the list.

    Returns:
        List of FVG objects, oldest-first.
    """
    result: list[FVG] = []
    n = len(bars)
    cutoff = max(0, n - max_age_bars - 2)  # -2 because a 3-bar pattern needs i+2

    for i in range(cutoff, n - 2):
        b0, b2 = bars[i], bars[i + 2]

        # Bullish FVG: b2.low > b0.high
        gap = b2.low - b0.high
        if gap >= min_gap_size:
            result.append(FVG(
                kind="bullish",
                top=b2.low,
                bottom=b0.high,
                mid=round((b2.low + b0.high) / 2, 4),
                gap_size=round(gap, 4),
                anchor_bar_index=i,
                displacement_bar_index=i + 1,
            ))
            continue  # a bar can only be one kind of FVG anchor

        # Bearish FVG: b2.high < b0.low
        gap = b0.low - b2.high
        if gap >= min_gap_size:
            result.append(FVG(
                kind="bearish",
                top=b0.low,
                bottom=b2.high,
                mid=round((b0.low + b2.high) / 2, 4),
                gap_size=round(gap, 4),
                anchor_bar_index=i,
                displacement_bar_index=i + 1,
            ))

    return result


def find_fvgs_after(
    bars: list[Bar],
    after_bar_index: int,
    kind: str,
    min_gap_size: float = 0.05,
) -> list[FVG]:
    """Return FVGs of the given kind that were created after after_bar_index.

    Used to find a directional FVG created in the wake of a sweep.

    Args:
        bars:            Full bars list.
        after_bar_index: Only return FVGs whose anchor bar is > this index.
        kind:            "bullish" or "bearish".
        min_gap_size:    Minimum gap size.

    Returns:
        List of matching FVG objects, oldest-first.
    """
    all_fvgs = find_fvgs(bars, min_gap_size=min_gap_size, max_age_bars=len(bars))
    return [f for f in all_fvgs if f.anchor_bar_index > after_bar_index and f.kind == kind]
