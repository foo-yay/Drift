from __future__ import annotations

import pandas as pd

from drift.features.base import FeatureComputer, bars_to_df
from drift.models import Bar


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder's Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder smoothing = EMA with alpha = 1/period
    return tr.ewm(alpha=1 / period, adjust=False).mean()


class VolatilityFeatures(FeatureComputer):
    """Computes ATR and a volatility regime label.

    Computed fields:
        atr                   - Average True Range for the configured period
        atr_percent           - ATR expressed as a percentage of last close
        atr_vs_20_avg         - ratio of current ATR to its 20-bar average
                                (> 1.0 means expanding volatility)
        volatility_regime     - "low" | "normal" | "elevated" | "extreme"
    """

    def __init__(self, atr_period: int) -> None:
        self._atr_period = atr_period

    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        df = bars_to_df(bars)
        if df.empty or len(df) < self._atr_period + 20:
            return self._empty_result()

        atr_series = _atr(df["high"], df["low"], df["close"], self._atr_period)
        if atr_series.dropna().empty:
            return self._empty_result()

        atr = float(atr_series.iloc[-1])
        last_close = float(df["close"].iloc[-1])
        atr_pct = round((atr / last_close) * 100, 4) if last_close else 0.0

        # Compare current ATR to its own recent average (expansion detection)
        recent_atr = atr_series.dropna().iloc[-20:]
        avg_atr = float(recent_atr.mean()) if len(recent_atr) >= 5 else atr
        atr_ratio = round(atr / avg_atr, 3) if avg_atr else 1.0

        return {
            "atr": round(atr, 4),
            "atr_percent": atr_pct,
            "atr_vs_20_avg": atr_ratio,
            "volatility_regime": self._classify_regime(atr_ratio),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _classify_regime(self, atr_ratio: float) -> str:
        if atr_ratio < 0.7:
            return "low"
        if atr_ratio < 1.2:
            return "normal"
        if atr_ratio < 1.8:
            return "elevated"
        return "extreme"

    def _empty_result(self) -> dict[str, object]:
        return {
            "atr": None,
            "atr_percent": None,
            "atr_vs_20_avg": None,
            "volatility_regime": "unknown",
        }
