"""Tests for StopEngine and TargetEngine."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from drift.config.models import RiskSection
from drift.models import LLMDecision, MarketSnapshot
from drift.planning.stop_engine import StopEngine
from drift.planning.target_engine import TargetEngine


def _risk_config(**overrides) -> RiskSection:
    base = dict(
        min_confidence=65,
        min_reward_risk=1.8,
        max_signals_per_day=3,
        cooldown_minutes=15,
        max_stop_points=30.0,
        min_stop_points=6.0,
        atr_stop_floor_mult=0.8,
        atr_target_mult=1.8,
        max_hold_minutes_default=25,
        no_trade_during_high_impact_events=True,
    )
    base.update(overrides)
    return RiskSection(**base)


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        as_of=datetime(2026, 4, 14, 14, 0, tzinfo=timezone.utc),
        symbol="MNQ",
        last_price=19500.0,
        session="open",
        bars_1m_count=180,
        bars_5m_count=120,
        bars_1h_count=72,
        trend_score=65,
        momentum_score=58,
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
    )


def _long_decision(**overrides) -> LLMDecision:
    base = dict(
        decision="LONG",
        confidence=72,
        setup_type="pullback_continuation",
        thesis="Bullish",
        entry_style="buy_pullback",
        entry_zone=[19490.0, 19496.0],
        invalidation_hint="1m close below VWAP",
        hold_minutes=20,
        do_not_trade_if=[],
    )
    base.update(overrides)
    return LLMDecision(**base)


def _short_decision(**overrides) -> LLMDecision:
    base = dict(
        decision="SHORT",
        confidence=70,
        setup_type="failed_breakout_reversion",
        thesis="Bearish rejection",
        entry_style="sell_pullback",
        entry_zone=[19510.0, 19516.0],
        invalidation_hint="1m close above 19520",
        hold_minutes=15,
        do_not_trade_if=[],
    )
    base.update(overrides)
    return LLMDecision(**base)


class TestStopEngine:
    def test_long_stop_is_below_entry(self):
        eng = StopEngine(_risk_config())
        stop = eng.calculate(_snapshot(), _long_decision(), atr=10.0)
        assert stop is not None
        assert stop < 19490.0

    def test_short_stop_is_above_entry(self):
        eng = StopEngine(_risk_config())
        stop = eng.calculate(_snapshot(), _short_decision(), atr=10.0)
        assert stop is not None
        assert stop > 19516.0

    def test_clamps_to_min_when_stop_too_tight(self):
        # ATR-based stop will be < min_stop_points; engine should widen to exactly min
        eng = StopEngine(_risk_config(atr_stop_floor_mult=0.01, min_stop_points=6.0))
        stop = eng.calculate(_snapshot(), _long_decision(), atr=1.0)
        assert stop is not None
        entry_min = _long_decision().entry_zone[0]
        assert abs(entry_min - stop) == pytest.approx(6.0, abs=0.01)

    def test_returns_none_when_stop_too_wide(self):
        eng = StopEngine(_risk_config(max_stop_points=5.0, min_stop_points=1.0))
        stop = eng.calculate(_snapshot(), _long_decision(), atr=10.0)
        assert stop is None

    def test_returns_none_for_no_trade(self):
        eng = StopEngine(_risk_config())
        no_trade = LLMDecision(
            decision="NO_TRADE", confidence=0, setup_type="none", thesis="n",
            entry_style="no_entry", entry_zone=[0.0, 0.0], invalidation_hint="n",
            hold_minutes=1, do_not_trade_if=[]
        )
        assert eng.calculate(_snapshot(), no_trade, atr=10.0) is None

    def test_stop_is_rounded_to_two_decimals(self):
        eng = StopEngine(_risk_config())
        stop = eng.calculate(_snapshot(), _long_decision(), atr=10.0)
        if stop is not None:
            assert stop == round(stop, 2)

    def test_structural_stop_used_when_within_max(self):
        # inv_price 5 pts below entry_min → structural stop is ~7 pts (inv - 2 buffer)
        # max_stop_points=30 → structural is within limits and wider than ATR floor
        eng = StopEngine(_risk_config(atr_stop_floor_mult=0.01, max_stop_points=30.0), structure_buffer=2.0)
        decision = _long_decision(invalidation_price=19483.0)  # 7 pts below entry_min 19490
        stop = eng.calculate(_snapshot(), decision, atr=1.0)
        # structural = 19483 - 2 = 19481; dist from entry_min = 9 pts; within max → used
        assert stop is not None
        assert stop == pytest.approx(19481.0, abs=0.01)

    def test_structural_stop_falls_back_to_atr_when_too_wide(self):
        # inv_price 50 pts below entry_min → structural would exceed max_stop_points
        eng = StopEngine(_risk_config(max_stop_points=20.0, atr_stop_floor_mult=0.8), structure_buffer=2.0)
        decision = _long_decision(invalidation_price=19440.0)  # 50 pts below entry_min
        stop = eng.calculate(_snapshot(), decision, atr=10.0)
        # structural = 19440 - 2 = 19438 → 52 pts > max 20 → falls back to ATR floor - buffer
        # atr_floor = 19490 - 8 = 19482; stop = 19482 - 2 = 19480
        assert stop is not None
        assert stop == pytest.approx(19480.0, abs=0.01)

    def test_structural_short_stop_used_when_within_max(self):
        eng = StopEngine(_risk_config(atr_stop_floor_mult=0.01, max_stop_points=30.0), structure_buffer=2.0)
        decision = _short_decision(invalidation_price=19523.0)  # 7 pts above entry_max 19516
        stop = eng.calculate(_snapshot(), decision, atr=1.0)
        # structural = 19523 + 2 = 19525; dist from entry_max = 9 pts → used
        assert stop is not None
        assert stop == pytest.approx(19525.0, abs=0.01)


class TestTargetEngine:
    def test_tp1_above_entry_for_long(self):
        eng = TargetEngine(_risk_config())
        tp1, tp2, rr = eng.calculate(_long_decision(), stop_loss=19481.0)
        assert tp1 > 19490.0

    def test_tp1_below_entry_for_short(self):
        eng = TargetEngine(_risk_config())
        tp1, tp2, rr = eng.calculate(_short_decision(), stop_loss=19525.0)
        assert tp1 < 19510.0

    def test_rr_equals_atr_target_mult(self):
        cfg = _risk_config(atr_target_mult=1.8)
        eng = TargetEngine(cfg)
        _, _, rr = eng.calculate(_long_decision(), stop_loss=19481.0)
        assert rr == 1.8

    def test_tp2_present_when_confidence_high(self):
        eng = TargetEngine(_risk_config())
        decision = _long_decision(confidence=75)
        _, tp2, _ = eng.calculate(decision, stop_loss=19481.0)
        assert tp2 is not None

    def test_tp2_none_when_confidence_low(self):
        eng = TargetEngine(_risk_config())
        decision = _long_decision(confidence=60)
        _, tp2, _ = eng.calculate(decision, stop_loss=19481.0)
        assert tp2 is None
