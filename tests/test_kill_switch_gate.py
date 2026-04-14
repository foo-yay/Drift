"""Tests for KillSwitchGate."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift.config.models import GatesSection
from drift.gates.kill_switch_gate import KillSwitchGate
from drift.models import MarketSnapshot


def _make_config(enabled: bool = True, path: str = "") -> GatesSection:
    return GatesSection(
        regime_enabled=True,
        min_trend_score=35,
        min_momentum_score=30,
        block_on_extreme_volatility=True,
        cooldown_enabled=True,
        kill_switch_enabled=enabled,
        kill_switch_path=path,
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


class TestKillSwitchGateDisabled:
    def test_disabled_passes_even_when_file_exists(self, tmp_path):
        ks_path = tmp_path / ".kill_switch"
        ks_path.touch()
        gate = KillSwitchGate(_make_config(enabled=False, path=str(ks_path)))
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "disabled" in result.reason.lower()


class TestKillSwitchGateActive:
    def test_blocks_when_kill_switch_file_exists(self, tmp_path):
        ks_path = tmp_path / ".kill_switch"
        ks_path.touch()
        gate = KillSwitchGate(_make_config(path=str(ks_path)))
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "active" in result.reason.lower()

    def test_reason_mentions_resume_command(self, tmp_path):
        ks_path = tmp_path / ".kill_switch"
        ks_path.touch()
        gate = KillSwitchGate(_make_config(path=str(ks_path)))
        result = gate.evaluate(_make_snapshot())
        assert "drift resume" in result.reason


class TestKillSwitchGateClear:
    def test_passes_when_file_absent(self, tmp_path):
        ks_path = tmp_path / ".kill_switch"
        gate = KillSwitchGate(_make_config(path=str(ks_path)))
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_after_file_removed(self, tmp_path):
        ks_path = tmp_path / ".kill_switch"
        ks_path.touch()
        gate = KillSwitchGate(_make_config(path=str(ks_path)))
        assert not gate.evaluate(_make_snapshot()).passed

        ks_path.unlink()
        assert gate.evaluate(_make_snapshot()).passed  # same gate instance, re-checks disk
