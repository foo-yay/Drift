"""Tests for GateRunner — sequencing, short-circuit, and GateReport output."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from drift.gates.base import Gate
from drift.gates.runner import GateRunner
from drift.models import GateReport, GateResult, MarketSnapshot


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


def _passing_gate(name: str) -> Gate:
    gate = MagicMock(spec=Gate)
    gate.name = name
    gate.evaluate.return_value = GateResult(gate_name=name, passed=True, reason="ok")
    return gate


def _blocking_gate(name: str, reason: str = "blocked") -> Gate:
    gate = MagicMock(spec=Gate)
    gate.name = name
    gate.evaluate.return_value = GateResult(gate_name=name, passed=False, reason=reason)
    return gate


class TestGateRunnerAllPass:
    def test_all_passed_true_when_all_gates_pass(self):
        runner = GateRunner([_passing_gate("a"), _passing_gate("b"), _passing_gate("c")])
        report = runner.run(_make_snapshot())
        assert report.all_passed
        assert len(report.results) == 3

    def test_all_gates_evaluated_when_all_pass(self):
        gates = [_passing_gate("a"), _passing_gate("b"), _passing_gate("c")]
        GateRunner(gates).run(_make_snapshot())
        for g in gates:
            g.evaluate.assert_called_once()

    def test_empty_gate_list_passes(self):
        report = GateRunner([]).run(_make_snapshot())
        assert report.all_passed
        assert report.results == []


class TestGateRunnerShortCircuit:
    def test_all_passed_false_on_first_gate_failure(self):
        runner = GateRunner([_blocking_gate("a"), _passing_gate("b")])
        report = runner.run(_make_snapshot())
        assert not report.all_passed

    def test_short_circuits_on_first_failure(self):
        gates = [_blocking_gate("a"), _passing_gate("b"), _passing_gate("c")]
        runner = GateRunner(gates)
        report = runner.run(_make_snapshot())
        # Only gate "a" should have been evaluated.
        gates[0].evaluate.assert_called_once()
        gates[1].evaluate.assert_not_called()
        gates[2].evaluate.assert_not_called()
        assert len(report.results) == 1

    def test_results_only_contain_evaluated_gates(self):
        runner = GateRunner([_passing_gate("a"), _blocking_gate("b"), _passing_gate("c")])
        report = runner.run(_make_snapshot())
        assert len(report.results) == 2
        assert report.results[0].gate_name == "a"
        assert report.results[1].gate_name == "b"

    def test_blocking_reason_preserved_in_results(self):
        runner = GateRunner([_blocking_gate("kill_switch", reason="Kill switch is ACTIVE.")])
        report = runner.run(_make_snapshot())
        assert report.results[0].reason == "Kill switch is ACTIVE."


class TestGateRunnerMixedResults:
    def test_middle_gate_fails(self):
        runner = GateRunner([_passing_gate("a"), _blocking_gate("b"), _passing_gate("c")])
        report = runner.run(_make_snapshot())
        assert not report.all_passed
        assert report.results[0].passed
        assert not report.results[1].passed

    def test_last_gate_fails(self):
        runner = GateRunner([_passing_gate("a"), _passing_gate("b"), _blocking_gate("c")])
        report = runner.run(_make_snapshot())
        assert not report.all_passed
        assert len(report.results) == 3
