from __future__ import annotations

from drift.features.base import FeatureComputer, bars_to_df
from drift.models import Bar


class StructureFeatures(FeatureComputer):
    """Computes price structure, session extremes, and candle characteristics.

    Computed fields:
        rolling_high          - highest high over the lookback window
        rolling_low           - lowest low over the lookback window
        session_high          - highest high since RTH open (using 1m bars passed in)
        session_low           - lowest low since RTH open
        dist_to_rolling_high  - last close minus rolling_high (negative = below)
        dist_to_rolling_low   - last close minus rolling_low (positive = above)
        dist_to_session_high  - last close minus session_high
        dist_to_session_low   - last close minus session_low
        candle_body_size      - abs(close - open) of last bar
        candle_upper_wick     - high - max(open, close) of last bar
        candle_lower_wick     - min(open, close) - low of last bar
        candle_body_pct       - body as pct of total candle range (0–100)
        is_bullish_candle     - True if last bar closed higher than it opened
        structure_note        - short plain-text label describing structure context
    """

    def __init__(self, rolling_window: int = 20) -> None:
        """
        Args:
            rolling_window: Number of bars for rolling high/low calculation.
        """
        self._window = rolling_window

    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        df = bars_to_df(bars)
        if df.empty or len(df) < 2:
            return self._empty_result()

        window = min(self._window, len(df))
        rolling_high = float(df["high"].iloc[-window:].max())
        rolling_low = float(df["low"].iloc[-window:].min())

        last = df.iloc[-1]
        last_close = float(last["close"])
        last_open = float(last["open"])
        last_high = float(last["high"])
        last_low = float(last["low"])

        # Session extremes — use the full bar list as a proxy for the session
        # (the engine passes only bars from today's RTH session when using 1m data)
        session_high = float(df["high"].max())
        session_low = float(df["low"].min())

        body_size = round(abs(last_close - last_open), 4)
        upper_wick = round(last_high - max(last_close, last_open), 4)
        lower_wick = round(min(last_close, last_open) - last_low, 4)
        candle_range = last_high - last_low
        body_pct = round((body_size / candle_range) * 100, 1) if candle_range > 0 else 0.0

        structure_note = self._describe_structure(
            last_close, rolling_high, rolling_low, session_high, session_low
        )

        return {
            "rolling_high": rolling_high,
            "rolling_low": rolling_low,
            "session_high": session_high,
            "session_low": session_low,
            "dist_to_rolling_high": round(last_close - rolling_high, 4),
            "dist_to_rolling_low": round(last_close - rolling_low, 4),
            "dist_to_session_high": round(last_close - session_high, 4),
            "dist_to_session_low": round(last_close - session_low, 4),
            "candle_body_size": body_size,
            "candle_upper_wick": upper_wick,
            "candle_lower_wick": lower_wick,
            "candle_body_pct": body_pct,
            "is_bullish_candle": last_close > last_open,
            "structure_note": structure_note,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _describe_structure(
        self,
        price: float,
        rolling_high: float,
        rolling_low: float,
        session_high: float,
        session_low: float,
    ) -> str:
        total_range = rolling_high - rolling_low
        if total_range <= 0:
            return "insufficient range"

        position_pct = (price - rolling_low) / total_range

        near_high = abs(price - rolling_high) / total_range < 0.08
        near_low = abs(price - rolling_low) / total_range < 0.08
        at_session_high = abs(price - session_high) < 0.01 * price
        at_session_low = abs(price - session_low) < 0.01 * price

        if near_high and at_session_high:
            return "near session high — extended"
        if near_low and at_session_low:
            return "near session low — compressed"
        if near_high:
            return "near rolling resistance"
        if near_low:
            return "near rolling support"
        if position_pct > 0.6:
            return "upper range — mild extension"
        if position_pct < 0.4:
            return "lower range — mild support zone"
        return "mid-range"

    def _empty_result(self) -> dict[str, object]:
        return {
            "rolling_high": None,
            "rolling_low": None,
            "session_high": None,
            "session_low": None,
            "dist_to_rolling_high": None,
            "dist_to_rolling_low": None,
            "dist_to_session_high": None,
            "dist_to_session_low": None,
            "candle_body_size": None,
            "candle_upper_wick": None,
            "candle_lower_wick": None,
            "candle_body_pct": None,
            "is_bullish_candle": None,
            "structure_note": "no data",
        }
