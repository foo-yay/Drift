from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

from drift.features.base import FeatureComputer, bars_to_df
from drift.models import Bar

_ET = ZoneInfo("America/New_York")
_RTH_OPEN_HOUR = 9
_RTH_OPEN_MINUTE = 30


class VolumeFeatures(FeatureComputer):
    """Computes session VWAP, volume spike metrics, and volume imbalance.

    VWAP is computed from RTH session open (9:30 AM ET) each day.
    Bars from prior sessions are excluded from the VWAP calculation.

    Computed fields:
        vwap                  - session VWAP value (None if no RTH bars today)
        price_vs_vwap         - last close minus VWAP (positive = above VWAP)
        price_vs_vwap_pct     - same as a percentage of VWAP
        vwap_bars_used        - number of 1m bars included in VWAP today
        volume_spike_ratio    - latest bar volume / rolling mean volume
        volume_state          - "spike" | "elevated" | "normal" | "low"
        volume_imbalance      - buy/sell volume imbalance proxy over the last
                                ``imbalance_window`` bars, in range [-100, +100].
                                Positive = net buyer pressure; negative = net seller
                                pressure. Computed by treating up-bars (close > open)
                                as buy volume and down-bars (close < open) as sell
                                volume. Doji bars (close == open) contribute equally
                                to both sides. None when not enough bars are available.
    """

    def __init__(self, volume_spike_window: int, imbalance_window: int = 10) -> None:
        self._spike_window = volume_spike_window
        self._imbalance_window = imbalance_window

    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        df = bars_to_df(bars)
        if df.empty:
            return self._empty_result()

        # ------------------------------------------------------------------
        # Session VWAP — filter to bars since today's RTH open in ET
        # ------------------------------------------------------------------
        # Convert UTC index to ET for session filtering
        df_et = df.copy()
        df_et.index = df_et.index.tz_convert(_ET)

        today = df_et.index[-1].date()
        rth_start = pd.Timestamp(
            year=today.year,
            month=today.month,
            day=today.day,
            hour=_RTH_OPEN_HOUR,
            minute=_RTH_OPEN_MINUTE,
            tz=_ET,
        )
        session_df = df_et[df_et.index >= rth_start]

        vwap: float | None = None
        price_vs_vwap: float | None = None
        price_vs_vwap_pct: float | None = None
        vwap_bars_used = 0

        if not session_df.empty:
            typical_price = (session_df["high"] + session_df["low"] + session_df["close"]) / 3
            cum_tp_vol = (typical_price * session_df["volume"]).cumsum()
            cum_vol = session_df["volume"].cumsum()
            # Avoid division by zero on bars with zero volume
            with_volume = cum_vol[cum_vol > 0]
            if not with_volume.empty:
                vwap_series = cum_tp_vol[cum_vol > 0] / with_volume
                vwap = float(vwap_series.iloc[-1])
                last_close = float(df["close"].iloc[-1])
                price_vs_vwap = round(last_close - vwap, 4)
                price_vs_vwap_pct = round((price_vs_vwap / vwap) * 100, 4) if vwap else None
                vwap_bars_used = len(session_df)

        # ------------------------------------------------------------------
        # Volume spike detection
        # ------------------------------------------------------------------
        spike_ratio: float | None = None
        volume_state = "unknown"

        if len(df) >= 2:
            window = min(self._spike_window, len(df) - 1)
            rolling_mean = float(df["volume"].iloc[-(window + 1) : -1].mean())
            last_volume = float(df["volume"].iloc[-1])
            if rolling_mean > 0:
                spike_ratio = round(last_volume / rolling_mean, 3)
                volume_state = self._classify_volume(spike_ratio)

        return {
            "vwap": round(vwap, 4) if vwap is not None else None,
            "price_vs_vwap": price_vs_vwap,
            "price_vs_vwap_pct": price_vs_vwap_pct,
            "vwap_bars_used": vwap_bars_used,
            "volume_spike_ratio": spike_ratio,
            "volume_state": volume_state,
            "volume_imbalance": self._compute_imbalance(df),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_imbalance(self, df: pd.DataFrame) -> float | None:
        """Compute signed buy/sell volume imbalance proxy over the last N bars.

        Each bar contributes its full volume to the buy side if close > open,
        to the sell side if close < open, or half-and-half if close == open
        (doji). The result is normalised to [-100, +100].

        Returns None when fewer bars than the window are available.
        """
        window = self._imbalance_window
        if len(df) < window:
            return None

        recent = df.iloc[-window:]
        buy_vol = 0.0
        sell_vol = 0.0
        for _, row in recent.iterrows():
            vol = float(row["volume"])
            if row["close"] > row["open"]:
                buy_vol += vol
            elif row["close"] < row["open"]:
                sell_vol += vol
            else:
                buy_vol += vol / 2
                sell_vol += vol / 2

        total = buy_vol + sell_vol
        if total == 0:
            return 0.0
        return round((buy_vol - sell_vol) / total * 100, 1)

    def _classify_volume(self, ratio: float) -> str:
        if ratio >= 2.5:
            return "spike"
        if ratio >= 1.5:
            return "elevated"
        if ratio >= 0.6:
            return "normal"
        return "low"

    def _empty_result(self) -> dict[str, object]:
        return {
            "vwap": None,
            "price_vs_vwap": None,
            "price_vs_vwap_pct": None,
            "vwap_bars_used": 0,
            "volume_spike_ratio": None,
            "volume_state": "unknown",
            "volume_imbalance": None,
        }
