from __future__ import annotations

import pandas as pd

from drift.features.base import FeatureComputer, bars_to_df
from drift.models import Bar


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (same smoothing as TradingView default)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


class MomentumFeatures(FeatureComputer):
    """Computes RSI, MACD, and a derived momentum state label.

    Computed fields:
        rsi                   - RSI value (0–100)
        macd_histogram        - MACD histogram (fast EMA - slow EMA, minus signal line)
        macd_line             - the raw MACD line value
        macd_signal_line      - the signal line value
        macd_histogram_slope  - 1-bar change in histogram (acceleration/deceleration)
        momentum_state        - "strong_bullish" | "bullish" | "neutral" |
                                "bearish" | "strong_bearish"
    """

    def __init__(
        self,
        rsi_period: int,
        macd_fast: int,
        macd_slow: int,
        macd_signal: int,
    ) -> None:
        self._rsi_period = rsi_period
        self._macd_fast = macd_fast
        self._macd_slow = macd_slow
        self._macd_signal = macd_signal

    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        df = bars_to_df(bars)
        min_bars = max(self._rsi_period, self._macd_slow) + self._macd_signal + 5
        if df.empty or len(df) < min_bars:
            return self._empty_result()

        # RSI
        rsi_series = _rsi(df["close"], self._rsi_period).dropna()
        if rsi_series.empty:
            return self._empty_result()
        rsi = float(rsi_series.iloc[-1])

        # MACD
        ema_fast = _ema(df["close"], self._macd_fast)
        ema_slow = _ema(df["close"], self._macd_slow)
        macd_line_series = ema_fast - ema_slow
        signal_series = _ema(macd_line_series, self._macd_signal)
        hist_series = macd_line_series - signal_series

        if hist_series.dropna().empty:
            return self._empty_result()

        histogram = float(hist_series.iloc[-1])
        macd_line = float(macd_line_series.iloc[-1])
        signal_line = float(signal_series.iloc[-1])
        hist_clean = hist_series.dropna()
        hist_slope = (
            float(hist_clean.iloc[-1]) - float(hist_clean.iloc[-2])
            if len(hist_clean) >= 2
            else 0.0
        )

        momentum_state = self._classify_momentum(rsi, histogram, hist_slope)

        return {
            "rsi": round(rsi, 2),
            "macd_histogram": round(histogram, 4),
            "macd_line": round(macd_line, 4),
            "macd_signal_line": round(signal_line, 4),
            "macd_histogram_slope": round(hist_slope, 4),
            "momentum_state": momentum_state,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _classify_momentum(
        self, rsi: float, histogram: float, hist_slope: float
    ) -> str:
        bullish_signals = 0
        if rsi > 55:
            bullish_signals += 1
        if rsi > 65:
            bullish_signals += 1
        if histogram > 0:
            bullish_signals += 1
        if hist_slope > 0:
            bullish_signals += 1

        if bullish_signals == 4:
            return "strong_bullish"
        if bullish_signals == 3:
            return "bullish"
        if bullish_signals == 1:
            return "bearish"
        if bullish_signals == 0:
            return "strong_bearish"
        return "neutral"

    def _empty_result(self) -> dict[str, object]:
        return {
            "rsi": None,
            "macd_histogram": None,
            "macd_line": None,
            "macd_signal_line": None,
            "macd_histogram_slope": None,
            "momentum_state": "unknown",
        }
