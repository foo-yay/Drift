from __future__ import annotations

import pandas as pd

from drift.features.base import FeatureComputer, bars_to_df
from drift.models import Bar

# Minimum body-to-range ratio for a candle to be considered "impulsive"
_MIN_BODY_RATIO = 0.55
# An order block is "touched" when price returns within this fraction of the zone height
_TOUCH_TOLERANCE_RATIO = 0.25


class OrderBlockFeatures(FeatureComputer):
    """Detects order blocks from OHLCV bar data.

    An order block is the last opposing candle before a strong impulsive move
    that produced a Break of Structure (BOS). The body of that candle defines
    a high-interest supply/demand zone.

    Computed fields (returned as lists of dicts for the snapshot):
        order_blocks  - list of detected zones, each:
                        {direction, top, bottom, formed_at (ISO), is_fresh}
    """

    def __init__(self, lookback: int = 50, max_blocks: int = 3) -> None:
        self._lookback = lookback
        self._max_blocks = max_blocks

    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        df = bars_to_df(bars)
        if len(df) < 3:
            return {"order_blocks": []}

        df = df.tail(self._lookback).copy()
        blocks = []

        closes = df["close"].values
        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        indices = df.index

        last_close = float(closes[-1])

        for i in range(1, len(df)):
            # Detect a bullish BOS: strong up move after a bearish candle
            curr_close = float(closes[i])
            curr_open = float(opens[i])
            curr_range = float(highs[i]) - float(lows[i])
            if curr_range == 0:
                continue
            curr_body = abs(curr_close - curr_open)
            body_ratio = curr_body / curr_range

            # Bullish order block: bearish candle at i-1 followed by strong bull at i
            if (
                body_ratio >= _MIN_BODY_RATIO
                and curr_close > curr_open  # bull candle
                and float(closes[i - 1]) < float(opens[i - 1])  # prior candle bearish
            ):
                ob_top = max(float(opens[i - 1]), float(closes[i - 1]))
                ob_bottom = min(float(opens[i - 1]), float(closes[i - 1]))
                zone_height = ob_top - ob_bottom
                is_fresh = last_close > ob_bottom  # price hasn't closed back inside
                blocks.append({
                    "direction": "bullish",
                    "top": round(ob_top, 2),
                    "bottom": round(ob_bottom, 2),
                    "formed_at": indices[i - 1].isoformat(),
                    "is_fresh": is_fresh,
                })

            # Bearish order block: bullish candle at i-1 followed by strong bear at i
            elif (
                body_ratio >= _MIN_BODY_RATIO
                and curr_close < curr_open  # bear candle
                and float(closes[i - 1]) > float(opens[i - 1])  # prior candle bullish
            ):
                ob_top = max(float(opens[i - 1]), float(closes[i - 1]))
                ob_bottom = min(float(opens[i - 1]), float(closes[i - 1]))
                is_fresh = last_close < ob_top
                blocks.append({
                    "direction": "bearish",
                    "top": round(ob_top, 2),
                    "bottom": round(ob_bottom, 2),
                    "formed_at": indices[i - 1].isoformat(),
                    "is_fresh": is_fresh,
                })

        # Return the most recent N blocks only, newest-first
        blocks = list(reversed(blocks[-self._max_blocks * 2 :]))[: self._max_blocks]
        return {"order_blocks": blocks}
