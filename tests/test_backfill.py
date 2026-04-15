"""Tests for storage.backfill — outcome backfill engine."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from drift.models import SignalEvent
from drift.storage.backfill import backfill_outcomes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc)

_TRADE_PLAN = {
    "generated_at": "2024-06-03T14:29:00+00:00",
    "symbol": "MNQ=F",
    "bias": "LONG",
    "setup_type": "breakout",
    "confidence": 80,
    "entry_min": 19000.0,
    "entry_max": 19005.0,
    "stop_loss": 18990.0,
    "take_profit_1": 19020.0,
    "take_profit_2": 19040.0,
    "reward_risk_ratio": 2.0,
    "max_hold_minutes": 60,
    "thesis": "test thesis",
    "invalidation_conditions": [],
    "operator_instructions": [],
    "do_not_trade_if": [],
}


def _make_line(
    final_outcome: str = "TRADE_PLAN_ISSUED",
    replay_outcome=None,
    source: str = "live",
    trade_plan: dict | None = None,
) -> str:
    event = {
        "event_time": _TS.isoformat(),
        "symbol": "MNQ=F",
        "source": source,
        "final_outcome": final_outcome,
        "final_reason": "test",
        "replay_outcome": replay_outcome,
        "trade_plan": trade_plan or (_TRADE_PLAN if final_outcome == "TRADE_PLAN_ISSUED" else None),
    }
    return json.dumps(event)


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Edge cases — no candidates
# ---------------------------------------------------------------------------

class TestBackfillNoCandidates:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            backfill_outcomes(tmp_path / "missing.jsonl", "MNQ=F")

    def test_empty_log_returns_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text("", encoding="utf-8")
        resolved, skipped = backfill_outcomes(p, "MNQ=F")
        assert resolved == 0 and skipped == 0

    def test_no_trade_plan_issued_returns_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_log(p, [_make_line("BLOCKED"), _make_line("LLM_NO_TRADE")])
        resolved, skipped = backfill_outcomes(p, "MNQ=F")
        assert resolved == 0 and skipped == 0

    def test_already_resolved_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_log(
            p,
            [_make_line(replay_outcome={"outcome": "TP1_HIT", "pnl_points": 10.0})],
        )
        resolved, skipped = backfill_outcomes(p, "MNQ=F")
        assert resolved == 0 and skipped == 0

    def test_replay_source_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_log(p, [_make_line(source="replay")])
        resolved, skipped = backfill_outcomes(p, "MNQ=F")
        assert resolved == 0 and skipped == 0

    def test_dry_run_source_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_log(p, [_make_line(source="dry_run")])
        resolved, skipped = backfill_outcomes(p, "MNQ=F")
        assert resolved == 0 and skipped == 0


# ---------------------------------------------------------------------------
# Successful resolution (mocked yfinance)
# ---------------------------------------------------------------------------

class TestBackfillResolution:
    def _make_mock_df(self, signal_time: datetime) -> "pd.DataFrame":  # type: ignore[name-defined]
        """Build a minimal 1m bar DataFrame that will trigger TP1."""
        import pandas as pd

        from zoneinfo import ZoneInfo
        utc = ZoneInfo("UTC")
        # Two bars after the signal: bar 1 is neutral, bar 2 hits TP1 (high >= 19020)
        times = [
            signal_time.replace(minute=signal_time.minute + 1, tzinfo=None),
            signal_time.replace(minute=signal_time.minute + 2, tzinfo=None),
        ]
        idx = pd.DatetimeIndex(times, tz="UTC")
        df = pd.DataFrame(
            {
                "Open":   [19001.0, 19002.0],
                "High":   [19010.0, 19025.0],  # bar 2 hits TP1 at 19020
                "Low":    [18999.0, 19000.0],
                "Close":  [19005.0, 19020.0],
                "Volume": [100, 200],
            },
            index=idx,
        )
        return df

    def test_resolves_tp1_and_patches_log(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_log(p, [_make_line(source="live")])

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = self._make_mock_df(_TS)

        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.return_value = mock_ticker
            resolved, skipped = backfill_outcomes(p, "MNQ=F")

        assert resolved == 1
        assert skipped == 0

        # Verify the JSONL file was patched
        line = json.loads(p.read_text(encoding="utf-8").strip())
        assert line["replay_outcome"] is not None
        assert line["replay_outcome"]["outcome"] == "TP1_HIT"
        assert line["replay_outcome"]["pnl_points"] > 0

    def test_non_live_lines_preserved_unmodified(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        blocked_line = _make_line("BLOCKED", source="live")
        live_tpi = _make_line(source="live")
        _write_log(p, [blocked_line, live_tpi])

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = self._make_mock_df(_TS)

        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.return_value = mock_ticker
            resolved, skipped = backfill_outcomes(p, "MNQ=F")

        assert resolved == 1
        lines = [json.loads(l) for l in p.read_text(encoding="utf-8").strip().splitlines()]
        assert lines[0]["final_outcome"] == "BLOCKED"
        assert lines[0].get("replay_outcome") is None
        assert lines[1]["replay_outcome"]["outcome"] == "TP1_HIT"

    def test_yfinance_failure_counts_as_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        _write_log(p, [_make_line(source="live")])

        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.return_value.history.side_effect = RuntimeError("network error")
            resolved, skipped = backfill_outcomes(p, "MNQ=F")

        assert resolved == 0
        assert skipped == 1
        # Original file must be untouched
        line = json.loads(p.read_text(encoding="utf-8").strip())
        assert line["replay_outcome"] is None

    def test_empty_bars_counts_as_skipped(self, tmp_path: Path) -> None:
        import pandas as pd

        p = tmp_path / "events.jsonl"
        _write_log(p, [_make_line(source="live")])

        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.return_value.history.return_value = pd.DataFrame()
            resolved, skipped = backfill_outcomes(p, "MNQ=F")

        assert resolved == 0
        assert skipped == 1


# ---------------------------------------------------------------------------
# Source field on SignalEvent
# ---------------------------------------------------------------------------

class TestSignalEventSource:
    def test_default_source_is_live(self) -> None:
        event = SignalEvent(
            event_time=_TS,
            symbol="MNQ=F",
            final_outcome="BLOCKED",
            final_reason="test",
        )
        assert event.source == "live"

    def test_explicit_replay_source(self) -> None:
        event = SignalEvent(
            event_time=_TS,
            symbol="MNQ=F",
            source="replay",
            final_outcome="BLOCKED",
            final_reason="test",
        )
        assert event.source == "replay"

    def test_explicit_dry_run_source(self) -> None:
        event = SignalEvent(
            event_time=_TS,
            symbol="MNQ=F",
            source="dry_run",
            final_outcome="LLM_NO_TRADE",
            final_reason="test",
        )
        assert event.source == "dry_run"

    def test_legacy_jsonl_without_source_defaults_to_live(self) -> None:
        """Events written before the source field was added must parse as 'live'."""
        raw = {
            "event_time": _TS.isoformat(),
            "symbol": "MNQ=F",
            "final_outcome": "BLOCKED",
            "final_reason": "legacy",
        }
        event = SignalEvent.model_validate(raw)
        assert event.source == "live"

    def test_invalid_source_value_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SignalEvent(
                event_time=_TS,
                symbol="MNQ=F",
                source="unknown",  # type: ignore[arg-type]
                final_outcome="BLOCKED",
                final_reason="test",
            )
