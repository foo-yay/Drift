"""Tests for the volume imbalance proxy — VolumeFeatures computation
and the directional check in TradePlanBuilder."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from drift.features.volume import VolumeFeatures
from drift.models import Bar, LLMDecision, MarketSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 4, 14, 14, 0, tzinfo=timezone.utc)


def _bar(open_: float, close: float, volume: float = 100.0) -> Bar:
    high = max(open_, close) + 1.0
    low = min(open_, close) - 1.0
    return Bar(
        timestamp=_TS,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe="1m",
        symbol="MNQ",
    )


def _snapshot(imbalance: float | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        as_of=_TS,
        symbol="MNQ",
        last_price=19500.0,
        session="open",
        bars_1m_count=50,
        bars_5m_count=50,
        bars_1h_count=12,
        trend_score=65,
        momentum_score=60,
        volatility_score=50,
        extension_risk=30,
        structure_quality=60,
        pullback_quality=55,
        breakout_quality=40,
        mean_reversion_risk=25,
        session_alignment=70,
        short_trend_state="bullish",
        medium_trend_state="bullish",
        momentum_state="bullish",
        volatility_regime="normal",
        volume_imbalance=imbalance,
    )


def _decision(direction: str = "LONG", confidence: int = 72) -> LLMDecision:
    return LLMDecision(
        decision=direction,
        confidence=confidence,
        setup_type="pullback_continuation",
        thesis="test",
        entry_style="buy_pullback",
        entry_zone=[19490.0, 19500.0],
        invalidation_hint="close below VWAP",
        hold_minutes=20,
        do_not_trade_if=[],
    )


def _app_config(imbalance_enabled: bool = True, threshold: float = 30.0) -> MagicMock:
    cfg = MagicMock()
    cfg.instrument.allow_long = True
    cfg.instrument.allow_short = True
    cfg.risk.min_confidence = 65
    cfg.risk.min_reward_risk = 1.8
    cfg.risk.max_signals_per_day = 3
    cfg.risk.cooldown_minutes = 15
    cfg.risk.max_stop_points = 30.0
    cfg.risk.min_stop_points = 6.0
    cfg.risk.atr_stop_floor_mult = 0.8
    cfg.risk.atr_target_mult = 1.8
    cfg.risk.max_hold_minutes_default = 25
    cfg.risk.no_trade_during_high_impact_events = False
    cfg.strategy.allowed_setup_types = ["pullback_continuation"]
    cfg.strategy.structure_buffer_points = 2.0
    cfg.strategy.chase_buffer_points = 4.0
    cfg.strategy.extension_atr_threshold = 1.2
    cfg.gates.volume_imbalance_gate_enabled = imbalance_enabled
    cfg.gates.volume_imbalance_threshold = threshold
    return cfg


# ---------------------------------------------------------------------------
# VolumeFeatures._compute_imbalance tests
# ---------------------------------------------------------------------------

class TestVolumeImbalanceComputation:
    def _features(self, window: int = 5) -> VolumeFeatures:
        return VolumeFeatures(volume_spike_window=20, imbalance_window=window)

    def test_all_up_bars_returns_positive_100(self):
        bars = [_bar(100.0, 101.0) for _ in range(5)]
        vf = self._features(window=5)
        result = vf._compute_imbalance(__import__("pandas").DataFrame(
            [{"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
             for b in bars]
        ))
        assert result == 100.0

    def test_all_down_bars_returns_negative_100(self):
        bars = [_bar(101.0, 100.0) for _ in range(5)]
        vf = self._features(window=5)
        import pandas as pd
        df = pd.DataFrame(
            [{"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
             for b in bars]
        )
        result = vf._compute_imbalance(df)
        assert result == -100.0

    def test_doji_bars_return_zero(self):
        bars = [_bar(100.0, 100.0) for _ in range(5)]
        vf = self._features(window=5)
        import pandas as pd
        df = pd.DataFrame(
            [{"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
             for b in bars]
        )
        result = vf._compute_imbalance(df)
        assert result == 0.0

    def test_mixed_bars_produces_intermediate_value(self):
        # 4 up-bars, 1 down-bar — buy vol = 400, sell vol = 100 → (300/500)*100 = 60.0
        bars = [_bar(100.0, 101.0) for _ in range(4)] + [_bar(101.0, 100.0)]
        vf = self._features(window=5)
        import pandas as pd
        df = pd.DataFrame(
            [{"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
             for b in bars]
        )
        result = vf._compute_imbalance(df)
        assert result == 60.0

    def test_returns_none_when_fewer_bars_than_window(self):
        bars = [_bar(100.0, 101.0) for _ in range(4)]
        vf = self._features(window=10)
        import pandas as pd
        df = pd.DataFrame(
            [{"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
             for b in bars]
        )
        result = vf._compute_imbalance(df)
        assert result is None

    def test_volume_features_compute_includes_imbalance(self):
        """Full compute() path returns volume_imbalance key."""
        from zoneinfo import ZoneInfo
        import pandas as pd
        et = ZoneInfo("America/New_York")
        ts = datetime(2026, 4, 14, 10, 0, tzinfo=et)  # within RTH
        bars = []
        for i in range(15):
            from datetime import timedelta
            bars.append(Bar(
                timestamp=ts + timedelta(minutes=i),
                open=100.0, high=102.0, low=99.0, close=101.0,
                volume=100.0, timeframe="1m", symbol="MNQ",
            ))
        vf = VolumeFeatures(volume_spike_window=20, imbalance_window=10)
        result = vf.compute(bars)
        assert "volume_imbalance" in result
        assert result["volume_imbalance"] is not None

    def test_empty_bars_returns_none_imbalance(self):
        vf = VolumeFeatures(volume_spike_window=20, imbalance_window=5)
        result = vf.compute([])
        assert result["volume_imbalance"] is None


# ---------------------------------------------------------------------------
# TradePlanBuilder volume imbalance gate tests
# ---------------------------------------------------------------------------

class TestTradePlanBuilderImbalanceGate:
    def _builder(self, imbalance_enabled: bool = True, threshold: float = 30.0):
        from drift.planning.trade_plan_builder import TradePlanBuilder
        return TradePlanBuilder(_app_config(imbalance_enabled, threshold))

    def test_long_rejected_when_seller_pressure_exceeds_threshold(self):
        builder = self._builder(threshold=30.0)
        snap = _snapshot(imbalance=-50.0)  # strong seller pressure
        result = builder.build(snap, _decision("LONG"))
        assert result is None

    def test_short_rejected_when_buyer_pressure_exceeds_threshold(self):
        builder = self._builder(threshold=30.0)
        snap = _snapshot(imbalance=50.0)  # strong buyer pressure
        result = builder.build(snap, _decision("SHORT", confidence=72))

        assert result is None

    def test_long_passes_when_imbalance_within_threshold(self):
        builder = self._builder(threshold=30.0)
        snap = _snapshot(imbalance=-20.0)  # mild seller pressure — below threshold
        snap = snap.model_copy(update={"atr": 10.0})
        result = builder.build(snap, _decision("LONG"))
        assert result is not None

    def test_short_passes_when_imbalance_within_threshold(self):
        builder = self._builder(threshold=30.0)
        snap = _snapshot(imbalance=20.0)  # mild buyer pressure — below threshold
        snap = snap.model_copy(update={"atr": 10.0})
        result = builder.build(snap, _decision("SHORT", confidence=72))
        assert result is not None

    def test_gate_skipped_when_imbalance_is_none(self):
        """If feature engine couldn't compute imbalance (not enough bars), gate is skipped."""
        builder = self._builder(threshold=30.0)
        snap = _snapshot(imbalance=None)
        snap = snap.model_copy(update={"atr": 10.0})
        result = builder.build(snap, _decision("LONG"))
        assert result is not None  # gate skipped, not blocked

    def test_gate_disabled_allows_any_imbalance(self):
        builder = self._builder(imbalance_enabled=False, threshold=30.0)
        snap = _snapshot(imbalance=-99.0)  # would normally block a LONG
        snap = snap.model_copy(update={"atr": 10.0})
        result = builder.build(snap, _decision("LONG"))
        assert result is not None

    def test_exactly_at_threshold_is_not_blocked(self):
        """Boundary: imbalance == -threshold should pass (< is the comparison)."""
        builder = self._builder(threshold=30.0)
        snap = _snapshot(imbalance=-30.0)
        snap = snap.model_copy(update={"atr": 10.0})
        result = builder.build(snap, _decision("LONG"))
        assert result is not None

    def test_just_over_threshold_is_blocked(self):
        builder = self._builder(threshold=30.0)
        snap = _snapshot(imbalance=-30.1)
        result = builder.build(snap, _decision("LONG"))
        assert result is None
