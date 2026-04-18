"""Tests for the structured position advisor."""
import json
from dataclasses import dataclass

import pytest

from drift.models import AssessmentRecommendation


# ------------------------------------------------------------------
# AssessmentRecommendation model tests
# ------------------------------------------------------------------

def test_hold_recommendation():
    rec = AssessmentRecommendation(action="HOLD", confidence=80, rationale="Momentum strong.")
    assert rec.action == "HOLD"
    assert rec.new_stop_loss is None
    assert rec.risk_flags == []


def test_adjust_recommendation():
    rec = AssessmentRecommendation(
        action="ADJUST",
        confidence=72,
        rationale="Tighten stop.",
        new_stop_loss=19000.0,
        new_take_profit_1=19050.0,
    )
    assert rec.action == "ADJUST"
    assert rec.new_stop_loss == 19000.0
    assert rec.new_take_profit_1 == 19050.0
    assert rec.new_take_profit_2 is None


def test_close_recommendation():
    rec = AssessmentRecommendation(
        action="CLOSE",
        confidence=90,
        rationale="Momentum dead.",
        risk_flags=["approaching resistance", "volume fading"],
    )
    assert rec.action == "CLOSE"
    assert len(rec.risk_flags) == 2


def test_invalid_action_rejected():
    with pytest.raises(Exception):
        AssessmentRecommendation(action="INVALID", confidence=50, rationale="x")


def test_confidence_bounds():
    with pytest.raises(Exception):
        AssessmentRecommendation(action="HOLD", confidence=101, rationale="x")
    with pytest.raises(Exception):
        AssessmentRecommendation(action="HOLD", confidence=-1, rationale="x")


def test_exit_mode_literal():
    rec = AssessmentRecommendation(
        action="ADJUST", confidence=60, rationale="Switch mode.",
        recommended_exit_mode="TP2",
    )
    assert rec.recommended_exit_mode == "TP2"

    with pytest.raises(Exception):
        AssessmentRecommendation(
            action="ADJUST", confidence=60, rationale="x",
            recommended_exit_mode="INVALID_MODE",
        )


def test_model_roundtrip():
    rec = AssessmentRecommendation(
        action="ADJUST", confidence=75, rationale="Tighten stop to breakeven.",
        new_stop_loss=19100.0, new_max_hold_minutes=45,
        risk_flags=["approaching resistance"],
    )
    data = json.loads(rec.model_dump_json())
    rec2 = AssessmentRecommendation.model_validate(data)
    assert rec2.new_stop_loss == 19100.0
    assert rec2.new_max_hold_minutes == 45


# ------------------------------------------------------------------
# JSON extraction tests (position_advisor._extract_json)
# ------------------------------------------------------------------

def test_extract_json_plain():
    from drift.ai.position_advisor import _extract_json

    raw = '{"action": "HOLD", "confidence": 80, "rationale": "looks good"}'
    result = _extract_json(raw)
    assert result["action"] == "HOLD"


def test_extract_json_fenced():
    from drift.ai.position_advisor import _extract_json

    raw = 'Here is my assessment:\n```json\n{"action": "ADJUST", "confidence": 70, "rationale": "tighten"}\n```'
    result = _extract_json(raw)
    assert result["action"] == "ADJUST"


def test_extract_json_with_preamble():
    from drift.ai.position_advisor import _extract_json

    raw = 'Let me analyze... {"action": "CLOSE", "confidence": 90, "rationale": "dead"} done.'
    result = _extract_json(raw)
    assert result["action"] == "CLOSE"


def test_extract_json_no_json_raises():
    from drift.ai.position_advisor import _extract_json

    with pytest.raises(ValueError, match="No JSON"):
        _extract_json("no json here at all")
