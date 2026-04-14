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


class TestCooldownGateDisabled:
    def test_disabled_always_passes(self, tmp_path):
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("SNAPSHOT_ONLY", minutes_ago=1)])
        gate = CooldownGate(_make_gates_config(cooldown_enabled=False), _make_risk_config(), log)
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "disabled" in result.reason.lower()


class TestCooldownGateZeroCooldown:
    def test_zero_cooldown_always_passes(self, tmp_path):
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("SNAPSHOT_ONLY", minutes_ago=0.1)])
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
        _write_log(log, [_event("SNAPSHOT_ONLY", minutes_ago=5)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "cooldown" in result.reason.lower()

    def test_reason_contains_remaining_minutes(self, tmp_path):
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("SNAPSHOT_ONLY", minutes_ago=5)])
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert "10" in result.reason  # ~10 min remaining


class TestCooldownGatePassing:
    def test_passes_when_signal_outside_window(self, tmp_path):
        """Signal 20 min ago, cooldown 15 min → passes."""
        log = tmp_path / "events.jsonl"
        _write_log(log, [_event("SNAPSHOT_ONLY", minutes_ago=20)])
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
                _event("SNAPSHOT_ONLY", minutes_ago=30),  # old — outside window
                _event("SNAPSHOT_ONLY", minutes_ago=3),   # recent — should block
            ],
        )
        gate = CooldownGate(_make_gates_config(), _make_risk_config(cooldown_minutes=15), log)
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
