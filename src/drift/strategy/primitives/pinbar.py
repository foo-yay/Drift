"""Pin bar confirmation detection — deterministic.

Definition
----------
A **bullish pin bar** has:
    1. A long lower wick relative to the total candle range.
    2. A small body relative to the total candle range.
    3. Close in the upper portion of the candle.

A **bearish pin bar** has:
    1. A long upper wick relative to the total candle range.
    2. A small body relative to the total candle range.
    3. Close in the lower portion of the candle.

Proxy assumptions (documented)
-------------------------------
- "Long wick" means the wick is >= wick_ratio * total_range.
  Default: 0.55 (wick is at least 55% of the total candle range).
- "Small body" means body_size <= max_body_ratio * total_range.
  Default: 0.35 (body is no more than 35% of total range).
- "Close in upper/lower portion" means the close is in the top/bottom
  close_zone_ratio of the candle.  Default: 0.4 (top/bottom 40%).
- If total_range == 0 (doji / no movement) the bar is not a pin bar.
- These thresholds are conservative enough to reduce false positives on
  equities while still firing on clear rejection candles.

All thresholds are configurable parameters so callers can tune per-instrument.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift.models import Bar


@dataclass(frozen=True)
class PinBarResult:
    kind: str           # "bullish" | "bearish"
    bar_index: int
    wick_ratio: float   # rejection wick / total range
    body_ratio: float   # body / total range
    close_zone: float   # normalised close position in range [0=low, 1=high]


def detect_pin_bar(
    bar: Bar,
    bar_index: int,
    min_wick_ratio: float = 0.55,
    max_body_ratio: float = 0.35,
    close_zone_ratio: float = 0.40,
) -> PinBarResult | None:
    """Evaluate a single bar for pin bar characteristics.

    Returns a PinBarResult if the bar qualifies as either a bullish or bearish
    pin bar, otherwise returns None.

    Args:
        bar:              The OHLCV bar to evaluate.
        bar_index:        Index of this bar in the parent bars list (for reference).
        min_wick_ratio:   Minimum fraction of total range that the rejection wick
                          must occupy.
        max_body_ratio:   Maximum fraction of total range that the body may occupy.
        close_zone_ratio: Fraction of the candle from the extreme that the close
                          must be within.  E.g. 0.40 means close must be in the
                          top 40% (bullish) or bottom 40% (bearish) of the range.

    Returns:
        PinBarResult | None
    """
    total_range = bar.high - bar.low
    if total_range <= 0:
        return None

    body_size = abs(bar.close - bar.open)
    body_ratio = body_size / total_range

    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low

    # Normalised close position: 0.0 = at the low, 1.0 = at the high
    close_pos = (bar.close - bar.low) / total_range

    # Bullish pin bar
    lower_wick_ratio = lower_wick / total_range
    if (
        lower_wick_ratio >= min_wick_ratio
        and body_ratio <= max_body_ratio
        and close_pos >= (1.0 - close_zone_ratio)  # close in upper portion
    ):
        return PinBarResult(
            kind="bullish",
            bar_index=bar_index,
            wick_ratio=round(lower_wick_ratio, 3),
            body_ratio=round(body_ratio, 3),
            close_zone=round(close_pos, 3),
        )

    # Bearish pin bar
    upper_wick_ratio = upper_wick / total_range
    if (
        upper_wick_ratio >= min_wick_ratio
        and body_ratio <= max_body_ratio
        and close_pos <= close_zone_ratio  # close in lower portion
    ):
        return PinBarResult(
            kind="bearish",
            bar_index=bar_index,
            wick_ratio=round(upper_wick_ratio, 3),
            body_ratio=round(body_ratio, 3),
            close_zone=round(close_pos, 3),
        )

    return None


def find_pin_bars_after(
    bars: list[Bar],
    after_bar_index: int,
    kind: str,
    min_wick_ratio: float = 0.55,
    max_body_ratio: float = 0.35,
    close_zone_ratio: float = 0.40,
) -> list[PinBarResult]:
    """Return all pin bars of the given kind after after_bar_index.

    Args:
        bars:            Full bars list.
        after_bar_index: Only examine bars with index > this value.
        kind:            "bullish" or "bearish".
        Other args:      Passed through to detect_pin_bar().

    Returns:
        List of PinBarResult objects for matching bars.
    """
    results: list[PinBarResult] = []
    for i in range(after_bar_index + 1, len(bars)):
        result = detect_pin_bar(
            bars[i],
            i,
            min_wick_ratio=min_wick_ratio,
            max_body_ratio=max_body_ratio,
            close_zone_ratio=close_zone_ratio,
        )
        if result is not None and result.kind == kind:
            results.append(result)
    return results
