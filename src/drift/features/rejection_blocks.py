from __future__ import annotations

from drift.features.base import FeatureComputer, bars_to_df
from drift.models import Bar

# A wick is "significant" if it is at least this fraction of the total bar range
_MIN_WICK_RATIO = 0.40
# Minimum total bar range in points (filters noise on tiny bars)
_MIN_RANGE_POINTS = 2.0


class RejectionBlockFeatures(FeatureComputer):
    """Detects rejection blocks — candles that show strong price rejection.

    A rejection block is a candle where one wick (upper or lower) is
    disproportionately large relative to the total bar range, indicating
    institutional-level rejection from a price zone.

    Computed fields:
        rejection_blocks  - list of detected zones, each:
                            {direction, level, wick_start, wick_end,
                             formed_at (ISO), strength_pct}
                            direction: "bearish_rejection" (upper wick)
                                     | "bullish_rejection" (lower wick)
    """

    def __init__(self, lookback: int = 30, max_blocks: int = 3) -> None:
        self._lookback = lookback
        self._max_blocks = max_blocks

    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        df = bars_to_df(bars)
        if len(df) < 1:
            return {"rejection_blocks": []}

        df = df.tail(self._lookback).copy()
        blocks = []

        for ts, row in df.iterrows():
            bar_range = row["high"] - row["low"]
            if bar_range < _MIN_RANGE_POINTS:
                continue

            body_top = max(row["open"], row["close"])
            body_bottom = min(row["open"], row["close"])
            upper_wick = row["high"] - body_top
            lower_wick = body_bottom - row["low"]

            upper_ratio = upper_wick / bar_range
            lower_ratio = lower_wick / bar_range

            if upper_ratio >= _MIN_WICK_RATIO:
                blocks.append({
                    "direction": "bearish_rejection",
                    "level": round(float(row["high"]), 2),
                    "wick_start": round(float(body_top), 2),
                    "wick_end": round(float(row["high"]), 2),
                    "formed_at": ts.isoformat(),
                    "strength_pct": round(upper_ratio * 100, 1),
                })

            if lower_ratio >= _MIN_WICK_RATIO:
                blocks.append({
                    "direction": "bullish_rejection",
                    "level": round(float(row["low"]), 2),
                    "wick_start": round(float(body_bottom), 2),
                    "wick_end": round(float(row["low"]), 2),
                    "formed_at": ts.isoformat(),
                    "strength_pct": round(lower_ratio * 100, 1),
                })

        # Newest-first, cap at max_blocks
        blocks = list(reversed(blocks))[: self._max_blocks]
        return {"rejection_blocks": blocks}
