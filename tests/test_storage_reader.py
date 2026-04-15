"""Tests for storage.reader — EventReader loading SignalEvents from JSONL."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift.models import SignalEvent
from drift.storage.reader import load_events_from_log


def _make_event(outcome: str = "BLOCKED") -> SignalEvent:
    return SignalEvent(
        event_time=datetime(2024, 1, 2, 14, 0, tzinfo=timezone.utc),
        symbol="MNQ",
        final_outcome=outcome,
        final_reason="test",
    )


def _serialise(event: SignalEvent) -> str:
    return json.dumps(event.model_dump(mode="json"))


class TestLoadEventsFromLog:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = load_events_from_log(tmp_path / "missing.jsonl")
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text("")
        assert load_events_from_log(p) == []

    def test_loads_single_event(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text(_serialise(_make_event("BLOCKED")) + "\n")
        result = load_events_from_log(p)
        assert len(result) == 1
        assert result[0].final_outcome == "BLOCKED"

    def test_loads_multiple_events_preserves_order(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        outcomes = ["BLOCKED", "LLM_NO_TRADE", "TRADE_PLAN_ISSUED"]
        p.write_text("\n".join(_serialise(_make_event(o)) for o in outcomes) + "\n")
        result = load_events_from_log(p)
        assert len(result) == 3
        assert [e.final_outcome for e in result] == outcomes

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        good = _serialise(_make_event("BLOCKED"))
        p.write_text(good + "\nnot json at all\n" + good + "\n")
        result = load_events_from_log(p)
        assert len(result) == 2

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text("\n" + _serialise(_make_event("LLM_NO_TRADE")) + "\n\n")
        result = load_events_from_log(p)
        assert len(result) == 1

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text(_serialise(_make_event()) + "\n")
        result = load_events_from_log(str(p))
        assert len(result) == 1

    def test_returns_signal_event_instances(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text(_serialise(_make_event("TRADE_PLAN_ISSUED")) + "\n")
        result = load_events_from_log(p)
        assert isinstance(result[0], SignalEvent)

    def test_event_fields_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        original = _make_event("LLM_NO_TRADE")
        p.write_text(_serialise(original) + "\n")
        loaded = load_events_from_log(p)[0]
        assert loaded.symbol == original.symbol
        assert loaded.final_outcome == original.final_outcome
        assert loaded.final_reason == original.final_reason
