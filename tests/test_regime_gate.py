"""Tests for RegimeGate — pass/fail cases for trend, momentum, and volatility."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from drift.config.models import GatesSection
from drift.gates.regime_gate import RegimeGate
from drift.models import MarketSnapshot


def _make_config(**overrides) -> GatesSection:
    defaults = {
        "regime_enabled": True,
        "min_trend_score": 35,
        "min_momentum_score": 30,
        "block_on_extreme_volatility": True,
        "cooldown_enabled": True,
        "kill_switch_enabled": True,
        "kill_switch_path": "data/.kill_switch",
    }
    defaults.update(overrides)
    return GatesSection(**defaults)


def _make_snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        as_of=datetime.now(tz=timezone.utc),
        symbol="MNQ",
        last_price=21_000.0,
        session="open",
        bars_1m_count=180,
        bars_5m_count=120,
        bars_1h_count=72,
        trend_score=60,
        momentum_score=55,
        volatility_score=60,
        extension_risk=20,
        structure_quality=50,
        pullback_quality=50,
        breakout_quality=50,
        mean_reversion_risk=20,
        session_alignment=50,
        short_trend_state="bullish",
        medium_trend_state="bullish",
        momentum_state="neutral",
        volatility_regime="normal",
    )
    base.update(overrides)
    return MarketSnapshot(**base)


class TestRegimeGateDisabled:
    def test_disabled_always_passes(self):
        gate = RegimeGate(_make_config(regime_enabled=False))
        result = gate.evaluate(_make_snapshot(trend_score=0, momentum_score=0))
        assert result.passed
        assert "disabled" in result.reason.lower()


class TestRegimeGateTrend:
    def test_blocks_when_trend_below_minimum(self):
        gate = RegimeGate(_make_config(min_trend_score=35))
        result = gate.evaluate(_make_snapshot(trend_score=34))
        assert not result.passed
        assert "trend" in result.reason.lower()

    def test_passes_at_trend_minimum(self):
        gate = RegimeGate(_make_config(min_trend_score=35))
        result = gate.evaluate(_make_snapshot(trend_score=35))
        assert result.passed

    def test_passes_when_trend_above_minimum(self):
        gate = RegimeGate(_make_config(min_trend_score=35))
        result = gate.evaluate(_make_snapshot(trend_score=80))
        assert result.passed


class TestRegimeGateMomentum:
    def test_blocks_when_momentum_below_minimum(self):
        gate = RegimeGate(_make_config(min_momentum_score=30))
        result = gate.evaluate(_make_snapshot(trend_score=50, momentum_score=29))
        assert not result.passed
        assert "momentum" in result.reason.lower()

    def test_passes_at_momentum_minimum(self):
        gate = RegimeGate(_make_config(min_momentum_score=30))
        result = gate.evaluate(_make_snapshot(trend_score=50, momentum_score=30))
        assert result.passed

    def test_trend_checked_before_momentum(self):
        """Trend failure should surface before momentum failure."""
        gate = RegimeGate(_make_config(min_trend_score=35, min_momentum_score=30))
        result = gate.evaluate(_make_snapshot(trend_score=10, momentum_score=5))
        assert not result.passed
        assert "trend" in result.reason.lower()


class TestRegimeGateVolatility:
    def test_blocks_on_extreme_volatility(self):
        gate = RegimeGate(_make_config(block_on_extreme_volatility=True))
        result = gate.evaluate(_make_snapshot(volatility_regime="extreme"))
        assert not result.passed
        assert "extreme" in result.reason.lower()

    def test_passes_on_elevated_volatility(self):
        gate = RegimeGate(_make_config(block_on_extreme_volatility=True))
        result = gate.evaluate(_make_snapshot(volatility_regime="elevated"))
        assert result.passed

    def test_extreme_not_blocked_when_flag_off(self):
        gate = RegimeGate(_make_config(block_on_extreme_volatility=False))
        result = gate.evaluate(_make_snapshot(volatility_regime="extreme"))
        assert result.passed

    def test_passes_normal_volatility(self):
        gate = RegimeGate(_make_config())
        result = gate.evaluate(_make_snapshot(volatility_regime="normal"))
        assert result.passed


class TestRegimeGatePassReason:
    def test_pass_reason_contains_scores(self):
        gate = RegimeGate(_make_config())
        result = gate.evaluate(_make_snapshot(trend_score=60, momentum_score=55))
        assert result.passed
        assert "60" in result.reason
        assert "55" in result.reason
