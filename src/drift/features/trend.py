from __future__ import annotations

import pandas as pd

from drift.features.base import FeatureComputer, bars_to_df
from drift.models import Bar


def _ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average using standard EMA (adjust=False for Wilder convention)."""
    return series.ewm(span=period, adjust=False).mean()


class TrendFeatures(FeatureComputer):
    """Computes EMA-based trend indicators and a short/medium trend state label.

    Computed fields:
        ema_{period}          - EMA value for each configured period
        price_vs_ema_fast     - last close minus the shortest EMA (signed)
        price_vs_ema_slow     - last close minus the longest EMA (signed)
        ema_slope_fast        - 1-bar change in the shortest EMA (direction proxy)
        ema_slope_slow        - 1-bar change in the longest EMA
        ema_spread            - shortest minus longest EMA (positive = bullish stack)
        short_trend_state     - "bullish" | "bearish" | "mixed" (1m frame label)
        medium_trend_state    - same labels but derived from 5m/1h context via EMAs
    """

    def __init__(self, ema_periods: list[int]) -> None:
        if not ema_periods:
            raise ValueError("ema_periods cannot be empty.")
        self._periods = sorted(ema_periods)

    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        df = bars_to_df(bars)
        if df.empty or len(df) < self._periods[-1]:
            return self._empty_result()

        results: dict[str, object] = {}
        ema_series: dict[int, pd.Series] = {}
        ema_values: dict[int, float] = {}

        for period in self._periods:
            s = _ema(df["close"], period)
            ema_series[period] = s
            ema_values[period] = float(s.iloc[-1])
            results[f"ema_{period}"] = ema_values[period]

        last_close = float(df["close"].iloc[-1])
        fast_period = self._periods[0]
        slow_period = self._periods[-1]
        fast_ema = ema_values[fast_period]
        slow_ema = ema_values[slow_period]

        results["price_vs_ema_fast"] = round(last_close - fast_ema, 4)
        results["price_vs_ema_slow"] = round(last_close - slow_ema, 4)
        results["ema_spread"] = round(fast_ema - slow_ema, 4)

        fast_s = ema_series[fast_period]
        slow_s = ema_series[slow_period]
        results["ema_slope_fast"] = round(float(fast_s.iloc[-1]) - float(fast_s.iloc[-2]), 4)
        results["ema_slope_slow"] = round(float(slow_s.iloc[-1]) - float(slow_s.iloc[-2]), 4)

        results["short_trend_state"] = self._classify_trend(
            last_close, ema_values, slope=float(results["ema_slope_fast"])
        )
        # Medium trend state re-uses the same logic but will be overridden by
        # the engine when 5m bars are passed to a second TrendFeatures instance.
        results["medium_trend_state"] = results["short_trend_state"]

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _classify_trend(
        self,
        price: float,
        ema_values: dict[int, float],
        slope: float,
    ) -> str:
        """Return a simple trend label based on EMA alignment and slope."""
        fast = ema_values[self._periods[0]]
        slow = ema_values[self._periods[-1]]
        above_fast = price > fast
        above_slow = price > slow
        ema_bullish_stack = fast > slow

        bullish_signals = sum([above_fast, above_slow, ema_bullish_stack, slope > 0])
        if bullish_signals >= 3:
            return "bullish"
        if bullish_signals <= 1:
            return "bearish"
        return "mixed"

    def _empty_result(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for period in self._periods:
            result[f"ema_{period}"] = None
        result.update(
            price_vs_ema_fast=None,
            price_vs_ema_slow=None,
            ema_spread=None,
            ema_slope_fast=None,
            ema_slope_slow=None,
            short_trend_state="unknown",
            medium_trend_state="unknown",
        )
        return result
