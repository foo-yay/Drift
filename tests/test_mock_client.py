"""Tests for MockLLMClient and dry-run wiring."""
from __future__ import annotations

from unittest.mock import MagicMock

from datetime import datetime, timezone

from drift.ai.mock_client import MockLLMClient
from drift.models import GateReport, GateResult, LLMDecision, MarketSnapshot


def _make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        as_of=datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc),
        symbol="MNQ=F",
        last_price=25_965.0,
        session="RTH",
        bars_1m_count=50,
        bars_5m_count=50,
        bars_1h_count=20,
        trend_score=72,
        momentum_score=65,
        volatility_score=55,
        extension_risk=20,
        structure_quality=68,
        pullback_quality=70,
        breakout_quality=60,
        mean_reversion_risk=18,
        session_alignment=80,
        short_trend_state="bullish",
        medium_trend_state="bullish",
        momentum_state="constructive",
        volatility_regime="normal",
    )


def _make_gate_report() -> GateReport:
    return GateReport(
        all_passed=True,
        results=[GateResult(gate_name="TestGate", passed=True, reason="ok")],
    )


class TestMockLLMClient:
    def test_returns_long_decision(self):
        client = MockLLMClient()
        snapshot = _make_snapshot()
        report = _make_gate_report()
        decision, raw_dict, raw_text = client.adjudicate(snapshot, report)

        assert isinstance(decision, LLMDecision)
        assert decision.decision == "LONG"

    def test_confidence_in_valid_range(self):
        client = MockLLMClient()
        snapshot = _make_snapshot()
        report = _make_gate_report()
        decision, _, _ = client.adjudicate(snapshot, report)

        assert 0 <= decision.confidence <= 100

    def test_entry_zone_is_two_element_list(self):
        client = MockLLMClient()
        snapshot = _make_snapshot()
        report = _make_gate_report()
        decision, _, _ = client.adjudicate(snapshot, report)

        assert isinstance(decision.entry_zone, list)
        assert len(decision.entry_zone) == 2
        assert decision.entry_zone[0] < decision.entry_zone[1]

    def test_raw_dict_matches_decision(self):
        client = MockLLMClient()
        snapshot = _make_snapshot()
        report = _make_gate_report()
        decision, raw_dict, _ = client.adjudicate(snapshot, report)

        assert raw_dict["decision"] == decision.decision
        assert raw_dict["confidence"] == decision.confidence

    def test_raw_text_label(self):
        client = MockLLMClient()
        snapshot = _make_snapshot()
        report = _make_gate_report()
        _, _, raw_text = client.adjudicate(snapshot, report)

        assert "mock" in raw_text.lower()

    def test_do_not_trade_if_is_list(self):
        client = MockLLMClient()
        snapshot = _make_snapshot()
        report = _make_gate_report()
        decision, _, _ = client.adjudicate(snapshot, report)

        assert isinstance(decision.do_not_trade_if, list)
        assert len(decision.do_not_trade_if) > 0

    def test_adjudicate_ignores_snapshot_content(self):
        """MockLLMClient always returns the same canned decision."""
        client = MockLLMClient()
        snapshot_a = _make_snapshot()
        snapshot_b = _make_snapshot()
        snapshot_b.last_price = 1.0  # wildly different price

        report = _make_gate_report()
        decision_a, _, _ = client.adjudicate(snapshot_a, report)
        decision_b, _, _ = client.adjudicate(snapshot_b, report)

        assert decision_a.decision == decision_b.decision
        assert decision_a.confidence == decision_b.confidence
