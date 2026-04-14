"""Tests for PromptBuilder."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from drift.ai.prompt_builder import PromptBuilder
from drift.models import GateReport, GateResult, MarketSnapshot


def _make_snapshot(**overrides) -> MarketSnapshot:
    base = dict(
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
    base.update(overrides)
    return MarketSnapshot(**base)


def _make_gate_report(passed: bool = True) -> GateReport:
    result = GateResult(gate_name="SessionGate", passed=passed, reason="In session.")
    return GateReport(all_passed=passed, results=[result])


@pytest.fixture()
def builder() -> PromptBuilder:
    return PromptBuilder()


class TestPromptBuilder:
    def test_returns_two_element_messages_list(self, builder):
        snap = _make_snapshot()
        report = _make_gate_report()
        messages = builder.build(snap, report)
        assert len(messages) == 1  # only user message; system is separate
        assert messages[0]["role"] == "user"

    def test_user_content_contains_symbol(self, builder):
        snap = _make_snapshot(symbol="MNQ")
        messages = builder.build(snap, _make_gate_report())
        assert "MNQ" in messages[0]["content"]

    def test_user_content_contains_last_price(self, builder):
        snap = _make_snapshot(last_price=19500.0)
        messages = builder.build(snap, _make_gate_report())
        assert "19500" in messages[0]["content"]

    def test_user_content_contains_valid_json(self, builder):
        snap = _make_snapshot()
        messages = builder.build(snap, _make_gate_report())
        content = messages[0]["content"]
        # The content should contain a JSON block
        start = content.find("{")
        end = content.rfind("}")
        assert start != -1 and end != -1
        payload = json.loads(content[start : end + 1])
        assert "regime_scores" in payload
        assert "gate_results" in payload

    def test_order_blocks_included_when_present(self, builder):
        snap = _make_snapshot(order_blocks=[{"direction": "bullish", "top": 19510.0, "bottom": 19500.0, "formed_at": "2026-04-14T13:00:00", "is_fresh": True}])
        messages = builder.build(snap, _make_gate_report())
        assert "order_blocks" in messages[0]["content"]

    def test_system_prompt_not_empty(self, builder):
        assert len(builder.system_prompt) > 100

    def test_system_prompt_contains_no_trade(self, builder):
        assert "NO_TRADE" in builder.system_prompt
