from __future__ import annotations

from datetime import datetime

from drift.config.models import AppConfig
from drift.features.momentum import MomentumFeatures
from drift.features.order_blocks import OrderBlockFeatures
from drift.features.rejection_blocks import RejectionBlockFeatures
from drift.features.structure import StructureFeatures
from drift.features.trend import TrendFeatures
from drift.features.volatility import VolatilityFeatures
from drift.features.volume import VolumeFeatures
from drift.models import Bar, MarketSnapshot


class FeatureEngine:
    """Coordinates all feature computers and assembles a MarketSnapshot.

    The engine is constructed once from config and then called on each cycle
    with fresh bar data. It is intentionally stateless between calls so that
    signals remain independently auditable.

    Architecture:
        - TrendFeatures   → run on 1m bars (short trend) and 5m bars (medium trend)
        - MomentumFeatures → run on 5m bars (best signal-to-noise for RSI/MACD)
        - VolatilityFeatures → run on 1m bars
        - VolumeFeatures  → run on 1m bars (session VWAP requires 1m resolution)
        - StructureFeatures → run on 5m bars
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        feat = config.features

        self._trend_short = TrendFeatures(ema_periods=feat.ema_periods)
        self._trend_medium = TrendFeatures(ema_periods=feat.ema_periods)
        self._momentum = MomentumFeatures(
            rsi_period=feat.rsi_period,
            macd_fast=feat.macd_fast,
            macd_slow=feat.macd_slow,
            macd_signal=feat.macd_signal,
        )
        self._volatility = VolatilityFeatures(atr_period=feat.atr_period)
        self._volume = VolumeFeatures(volume_spike_window=feat.volume_spike_window)
        self._structure = StructureFeatures(rolling_window=20)
        self._order_blocks = OrderBlockFeatures(lookback=50, max_blocks=3)
        self._rejection_blocks = RejectionBlockFeatures(lookback=30, max_blocks=3)

    def compute(
        self,
        bars_1m: list[Bar],
        bars_5m: list[Bar],
        bars_1h: list[Bar],  # reserved for future hourly context
        last_price: float,
        session: str,
        reference_time: datetime | None = None,
    ) -> MarketSnapshot:
        """Run all feature computers and return a scored MarketSnapshot.

        Args:
            bars_1m:     1-minute bars, oldest-first.
            bars_5m:     5-minute bars, oldest-first.
            bars_1h:     Hourly bars (used for future regime context).
            last_price:  Latest quote from the data provider.
            session:     Session label from the data provider.

        Returns:
            A fully populated MarketSnapshot ready for scoring and LLM input.
        """
        # ----------------------------------------------------------------
        # Run all computers
        # ----------------------------------------------------------------
        trend_short = self._trend_short.compute(bars_1m)
        trend_medium_raw = self._trend_medium.compute(bars_5m)
        momentum = self._momentum.compute(bars_5m)
        volatility = self._volatility.compute(bars_1m)
        volume = self._volume.compute(bars_1m)
        structure = self._structure.compute(bars_5m)
        order_block_data = self._order_blocks.compute(bars_5m)
        rejection_block_data = self._rejection_blocks.compute(bars_5m)

        # ----------------------------------------------------------------
        # Derive regime scores (0–100, higher = more favorable for trading)
        # ----------------------------------------------------------------
        trend_score = self._score_trend(trend_short, trend_medium_raw)
        momentum_score = self._score_momentum(momentum)
        volatility_score = self._score_volatility(volatility)
        extension_risk = self._score_extension(structure, volume, volatility)
        structure_quality = self._score_structure_quality(structure)
        pullback_quality = self._score_pullback(trend_short, momentum, structure)
        breakout_quality = self._score_breakout(momentum, volume, structure)
        mean_reversion_risk = self._score_mean_reversion(volatility, extension_risk, volume)
        session_alignment = self._score_session_alignment(session)

        short_trend_state = str(trend_short.get("short_trend_state", "unknown"))
        medium_trend_state = str(trend_medium_raw.get("short_trend_state", "unknown"))
        momentum_state = str(momentum.get("momentum_state", "unknown"))
        volatility_regime = str(volatility.get("volatility_regime", "unknown"))

        market_note = self._build_market_note(
            trend_short, momentum, volatility, volume, structure
        )

        return MarketSnapshot(
            as_of=bars_1m[-1].timestamp,
            symbol=self._config.instrument.symbol,
            last_price=last_price,
            session=session,
            bars_1m_count=len(bars_1m),
            bars_5m_count=len(bars_5m),
            bars_1h_count=len(bars_1h),
            trend_score=trend_score,
            momentum_score=momentum_score,
            volatility_score=volatility_score,
            extension_risk=extension_risk,
            structure_quality=structure_quality,
            pullback_quality=pullback_quality,
            breakout_quality=breakout_quality,
            mean_reversion_risk=mean_reversion_risk,
            session_alignment=session_alignment,
            short_trend_state=short_trend_state,
            medium_trend_state=medium_trend_state,
            momentum_state=momentum_state,
            volatility_regime=volatility_regime,
            order_blocks=order_block_data.get("order_blocks", []),
            rejection_blocks=rejection_block_data.get("rejection_blocks", []),
            atr=volatility.get("atr"),
            volume_imbalance=volume.get("volume_imbalance"),
            reference_time=reference_time,
            market_note=market_note,
        )

    # ------------------------------------------------------------------
    # Scoring helpers — each returns an int in [0, 100]
    # ------------------------------------------------------------------

    def _score_trend(
        self, short: dict[str, object], medium: dict[str, object]
    ) -> int:
        score = 50
        for ctx in (short, medium):
            state = ctx.get("short_trend_state", "unknown")
            if state == "bullish":
                score += 12
            elif state == "bearish":
                score -= 12
        spread = short.get("ema_spread")
        slope = short.get("ema_slope_fast")
        if isinstance(spread, float):
            score += 8 if spread > 0 else -8
        if isinstance(slope, float):
            score += 5 if slope > 0 else -5
        return max(0, min(100, score))

    def _score_momentum(self, m: dict[str, object]) -> int:
        score = 50
        state = m.get("momentum_state", "unknown")
        adjustments = {
            "strong_bullish": 35,
            "bullish": 20,
            "neutral": 0,
            "bearish": -20,
            "strong_bearish": -35,
        }
        score += adjustments.get(str(state), 0)
        hist = m.get("macd_histogram")
        if isinstance(hist, float):
            score += 5 if hist > 0 else -5
        return max(0, min(100, score))

    def _score_volatility(self, v: dict[str, object]) -> int:
        """Higher score = healthier, tradeable volatility (not too low, not extreme)."""
        regime = v.get("volatility_regime", "unknown")
        mapping = {"low": 30, "normal": 75, "elevated": 55, "extreme": 20}
        return mapping.get(str(regime), 50)

    def _score_extension(
        self,
        structure: dict[str, object],
        volume: dict[str, object],
        volatility: dict[str, object],
    ) -> int:
        """Extension risk — higher = more extended (less favorable for new entries)."""
        score = 30  # base low risk assumption
        dist_high = structure.get("dist_to_rolling_high")
        dist_low = structure.get("dist_to_rolling_low")
        atr = volatility.get("atr")

        if isinstance(dist_high, float) and isinstance(atr, float) and atr > 0:
            # close to the rolling high = extension risk
            proximity_pct = abs(dist_high) / atr
            if proximity_pct < 0.3:
                score += 40
            elif proximity_pct < 0.8:
                score += 20

        if isinstance(dist_low, float) and isinstance(atr, float) and atr > 0:
            proximity_pct = abs(dist_low) / atr
            if proximity_pct < 0.3:
                score += 40
            elif proximity_pct < 0.8:
                score += 20

        return max(0, min(100, score))

    def _score_structure_quality(self, structure: dict[str, object]) -> int:
        score = 50
        body_pct = structure.get("candle_body_pct")
        note = str(structure.get("structure_note", ""))

        if isinstance(body_pct, float):
            if body_pct > 60:
                score += 15
            elif body_pct < 25:
                score -= 15

        if "mid-range" in note:
            score += 10
        elif "extended" in note:
            score -= 20
        elif "support" in note or "low" in note:
            score += 5

        return max(0, min(100, score))

    def _score_pullback(
        self,
        trend: dict[str, object],
        momentum: dict[str, object],
        structure: dict[str, object],
    ) -> int:
        score = 40
        if str(trend.get("short_trend_state")) == "bullish":
            score += 20
        rsi = momentum.get("rsi")
        if isinstance(rsi, float):
            # Ideal pullback RSI: 40–55 in an uptrend
            if 40 <= rsi <= 58:
                score += 20
            elif rsi > 70:
                score -= 20
        dist_lo = structure.get("dist_to_rolling_low")
        atr = None  # not available here; structure-only check
        if isinstance(dist_lo, float) and dist_lo > 0:
            score += 10
        return max(0, min(100, score))

    def _score_breakout(
        self,
        momentum: dict[str, object],
        volume: dict[str, object],
        structure: dict[str, object],
    ) -> int:
        score = 30
        vol_state = str(volume.get("volume_state", "normal"))
        if vol_state == "spike":
            score += 30
        elif vol_state == "elevated":
            score += 15

        hist = momentum.get("macd_histogram")
        slope = momentum.get("macd_histogram_slope")
        if isinstance(hist, float) and hist > 0:
            score += 15
        if isinstance(slope, float) and slope > 0:
            score += 10

        dist_high = structure.get("dist_to_rolling_high")
        if isinstance(dist_high, float) and abs(dist_high) < 5:
            score += 15

        return max(0, min(100, score))

    def _score_mean_reversion(
        self,
        volatility: dict[str, object],
        extension_risk: int,
        volume: dict[str, object],
    ) -> int:
        """Higher = greater mean-reversion danger (chase risk)."""
        score = extension_risk // 2
        regime = str(volatility.get("volatility_regime", "normal"))
        if regime == "extreme":
            score += 25
        elif regime == "elevated":
            score += 10
        vol_state = str(volume.get("volume_state", "normal"))
        if vol_state == "spike":
            score += 15
        return max(0, min(100, score))

    def _score_session_alignment(self, session: str) -> int:
        mapping = {"open": 80, "pre-market": 30, "after-hours": 20}
        return mapping.get(session, 40)

    # ------------------------------------------------------------------
    # Human-readable market note
    # ------------------------------------------------------------------

    def _build_market_note(
        self,
        trend: dict[str, object],
        momentum: dict[str, object],
        volatility: dict[str, object],
        volume: dict[str, object],
        structure: dict[str, object],
    ) -> str:
        parts = []
        short_state = trend.get("short_trend_state", "unknown")
        mom_state = momentum.get("momentum_state", "unknown")
        vol_regime = volatility.get("volatility_regime", "unknown")
        vol_state = volume.get("volume_state", "normal")
        struct_note = structure.get("structure_note", "")
        vwap_diff = volume.get("price_vs_vwap")

        parts.append(f"Trend: {short_state}.")
        parts.append(f"Momentum: {mom_state}.")
        parts.append(f"Volatility: {vol_regime}.")

        if vol_state in ("spike", "elevated"):
            parts.append(f"Volume: {vol_state}.")

        if isinstance(vwap_diff, float):
            side = "above" if vwap_diff >= 0 else "below"
            parts.append(f"Price {side} VWAP by {abs(vwap_diff):.2f}.")

        if struct_note:
            parts.append(f"Structure: {struct_note}.")

        return " ".join(parts)
