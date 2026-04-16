"""Tests for CooldownGate."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drift.config.models import GatesSection, RiskSection
from drift.gates.cooldown_gate import CooldownGate
from drift.models import MarketSnapshot


def _make_gates_config(cooldown_enabled: bool = True) -> GatesSection:
    return GatesSection(
        regime_enabled=True,
        min_trend_score=35,
        min_momentum_score=30,
        block_on_extreme_volatility=True,
        cooldown_enabled=cooldown_enabled,
        kill_switch_enabled=True,
        kill_switch_path="data/.kill_switch",
    )


def _make_risk_config(cooldown_minutes: int = 15) -> RiskSection:
    return RiskSection(
        min_confidence=65,
        min_reward_risk=1.8,
        max_signals_per_day=3,
        cooldown_minutes=cooldown_minutes,
        max_stop_points=30.0,
        min_stop_points=6.0,
        atr_stop_floor_mult=0.8,
        atr_target_mult=1.8,
        max_hold_minutes_default=25,
        no_trade_during_high_impact_events=True,
    )


def _make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
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


def _write_log(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _event(final_outcome: str, minutes_ago: float) -> dict:
    ts = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)
    return {"event_time": ts.isoformat(), "final_outcome": final_outcome}


def _event_with_plan(minutes_ago: float, max_hold_minutes: int) -> dict:
    """TRADE_PLAN_ISSUED event that includes a trade_plan.max_hold_minutes field."""
    ts = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "event_time": ts.isoformat(),
        "final_outcome": "TRADE_PLAN_ISSUED",
        "trade_plan": {"max_hold_minutes": max_hold_minutes},
    }


class TestCooldownGateDisabled:
    def test_disabled_always_passes(self, tmp_path):
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=1)])
        gate = CooldownGate(_make_gates_config(cooldown_enabled=False), _make_risk_config(), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "disabled" in result.reason.lower()


class TestCooldownGateZeroCooldown:
    def test_zero_cooldown_always_passes(self, tmp_path):
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=0.1)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=0), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed


class TestCooldownGateNoLog:
    def test_passes_when_log_does_not_exist(self, tmp_path):
        log = tmp_path / "nonexistent.jsonl"
        gate = CooldownGate(_make_gates_config(), _make_risk_config(), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "no previous" in result.reason.lower()

    def test_passes_when_log_is_empty(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("")
        gate = CooldownGate(_make_gates_config(), _make_risk_config(), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed


class TestCooldownGateBlocking:
    def test_blocks_when_recent_signal(self, tmp_path):
        """Signal 5 min ago, cooldown 15 min → blocked."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=5)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "cooldown" in result.reason.lower()

    def test_reason_contains_remaining_minutes(self, tmp_path):
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=5)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert "10" in result.reason  # ~10 min remaining


class TestCooldownGatePassing:
    def test_passes_when_signal_outside_window(self, tmp_path):
        """Signal 20 min ago, cooldown 15 min → passes."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=20)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_blocked_outcomes_excluded_from_cooldown(self, tmp_path):
        """BLOCKED events should not count towards the cooldown timer."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("BLOCKED", minutes_ago=1)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "no previous" in result.reason.lower()

    def test_uses_most_recent_signal(self, tmp_path):
        """With multiple signal events, only the most recent matters."""
        log = tmp_path / "events.jsonl"
        _write_log(
            log,
            [
                _event("TRADE_PLAN_ISSUED", minutes_ago=30),  # old — outside window
                _event("TRADE_PLAN_ISSUED", minutes_ago=3),   # recent — should block
            ],
        )
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert not result.passed


class TestCooldownGateDynamicHoldMinutes:
    """Cooldown window is driven by trade_plan.max_hold_minutes from the JSONL event."""

    def test_uses_max_hold_minutes_from_trade_plan(self, tmp_path):
        """Plan with 30-min hold: signal 20 min ago should still be blocked."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event_with_plan(minutes_ago=20, max_hold_minutes=30)])
        # Config says 15 min — but plan says 30 min, so 20 min ago must block.
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "30" in result.reason  # window shown in reason

    def test_plan_hold_minutes_shorter_than_config(self, tmp_path):
        """Plan with 10-min hold: signal 12 min ago should pass even though config is 15."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event_with_plan(minutes_ago=12, max_hold_minutes=10)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_falls_back_to_config_when_trade_plan_absent(self, tmp_path):
        """Events without trade_plan field fall back to risk.cooldown_minutes."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=5)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert not result.passed  # 5 min ago < 15 min config → still blocked

    def test_most_recent_plan_hold_minutes_wins(self, tmp_path):
        """When multiple events exist, hold_minutes comes from the most-recent one."""
        log = tmp_path / "events.jsonl"
        _write_log(
            log,
            [
                _event_with_plan(minutes_ago=40, max_hold_minutes=60),  # old, irrelevant
                _event_with_plan(minutes_ago=5,  max_hold_minutes=10),  # recent — 10-min hold
            ],
        )
        # Signal 5 min ago, hold=10 min → 5 min remaining → blocked
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "10" in result.reason  # 10-min window shown


class TestCooldownGateSecondsRemaining:
    def test_returns_none_when_no_log(self, tmp_path):
        log = tmp_path / "events.jsonl"
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        assert gate.seconds_remaining() is None

    def test_returns_none_when_cooldown_clear(self, tmp_path):
        """Signal 20 min ago, cooldown 15 min → already clear → None."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=20)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        assert gate.seconds_remaining() is None

    def test_returns_positive_seconds_when_active(self, tmp_path):
        """Signal 5 min ago, cooldown 15 min → ~600 seconds remaining."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=5)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        remaining = gate.seconds_remaining()
        assert remaining is not None
        assert 550 < remaining < 620  # ~10 min ± small timing tolerance

    def test_returns_none_when_disabled(self, tmp_path):
        """Gate disabled → seconds_remaining always None."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("TRADE_PLAN_ISSUED", minutes_ago=1)])
        gate = CooldownGate(_make_gates_config(cooldown_enabled=False), _make_risk_config(), log)
        assert gate.seconds_remaining() is None

    def test_uses_max_hold_minutes_for_remaining_calculation(self, tmp_path):
        """Remaining seconds reflect max_hold_minutes, not config cooldown_minutes."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event_with_plan(minutes_ago=5, max_hold_minutes=30)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        remaining = gate.seconds_remaining()
        assert remaining is not None
        # 30 min hold − 5 min elapsed = ~25 min = ~1500 s
        assert 1450 < remaining < 1560
