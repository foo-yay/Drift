"""Tests for ResponseParser."""
from __future__ import annotations

import json

import pytest

from drift.ai.response_parser import ResponseParser, _NO_TRADE_FALLBACK


@pytest.fixture()
def parser() -> ResponseParser:
    return ResponseParser()


def _make_valid_payload(**overrides) -> dict:
    base = {
        "decision": "LONG",
        "confidence": 72,
        "setup_type": "pullback_continuation",
        "thesis": "Bullish trend with constructive pullback.",
        "entry_style": "buy_pullback",
        "entry_zone": [19500.0, 19506.0],
        "invalidation_hint": "1m close below VWAP",
        "hold_minutes": 20,
        "do_not_trade_if": ["price extends beyond zone"],
    }
    base.update(overrides)
    return base


class TestResponseParserHappyPath:
    def test_parses_valid_json(self, parser):
        raw = json.dumps(_make_valid_payload())
        decision, raw_dict = parser.parse(raw)
        assert decision.decision == "LONG"
        assert decision.confidence == 72
        assert decision.entry_zone == [19500.0, 19506.0]

    def test_parses_json_with_markdown_fence(self, parser):
        payload = json.dumps(_make_valid_payload())
        raw = f"```json\n{payload}\n```"
        decision, _ = parser.parse(raw)
        assert decision.decision == "LONG"

    def test_parses_short_response(self, parser):
        payload = _make_valid_payload(decision="NO_TRADE", entry_zone=[0.0, 0.0], hold_minutes=1)
        raw = json.dumps(payload)
        decision, _ = parser.parse(raw)
        assert decision.decision == "NO_TRADE"

    def test_parses_json_with_surrounding_text(self, parser):
        payload = json.dumps(_make_valid_payload(decision="SHORT"))
        raw = f"Here is my analysis:\n{payload}\nEnd of response."
        decision, _ = parser.parse(raw)
        assert decision.decision == "SHORT"

    def test_returns_raw_dict(self, parser):
        payload = _make_valid_payload()
        decision, raw_dict = parser.parse(json.dumps(payload))
        assert raw_dict["setup_type"] == "pullback_continuation"


class TestResponseParserFallbacks:
    def test_returns_no_trade_on_empty_string(self, parser):
        decision, raw_dict = parser.parse("")
        assert decision.decision == "NO_TRADE"
        assert raw_dict == {}

    def test_returns_no_trade_on_invalid_json(self, parser):
        decision, _ = parser.parse("not json at all")
        assert decision is _NO_TRADE_FALLBACK

    def test_returns_no_trade_on_missing_required_field(self, parser):
        payload = _make_valid_payload()
        del payload["decision"]
        decision, _ = parser.parse(json.dumps(payload))
        assert decision is _NO_TRADE_FALLBACK

    def test_returns_no_trade_on_invalid_decision_value(self, parser):
        payload = _make_valid_payload(decision="MAYBE")
        decision, _ = parser.parse(json.dumps(payload))
        assert decision is _NO_TRADE_FALLBACK

    def test_returns_no_trade_on_confidence_out_of_range(self, parser):
        payload = _make_valid_payload(confidence=150)
        decision, _ = parser.parse(json.dumps(payload))
        assert decision is _NO_TRADE_FALLBACK

    def test_no_trade_fallback_is_safe(self):
        fb = _NO_TRADE_FALLBACK
        assert fb.decision == "NO_TRADE"
        assert fb.confidence == 0
        assert fb.entry_zone == [0.0, 0.0]
