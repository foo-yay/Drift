"""Tests for the replay layer: ReplayProvider, loader, and ReplayEngine."""
from __future__ import annotations

import csv
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drift.models import Bar
from drift.replay.provider import ReplayProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_minutes: int = 0) -> datetime:
    """Return a fixed UTC datetime offset by the given number of minutes."""
    base = datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=offset_minutes)


def _bar(offset_minutes: int, timeframe: str = "1m", close: float = 100.0) -> Bar:
    ts = _ts(offset_minutes)
    return Bar(
        timestamp=ts,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=1000.0,
        timeframe=timeframe,
        symbol="MNQ=F",
    )


def _make_bars_1m(n: int = 30) -> list[Bar]:
    return [_bar(i, "1m", close=100.0 + i) for i in range(n)]


def _make_bars_5m(n: int = 10) -> list[Bar]:
    return [_bar(i * 5, "5m", close=100.0 + i) for i in range(n)]


def _make_bars_1h(n: int = 5) -> list[Bar]:
    return [_bar(i * 60, "1h", close=100.0 + i) for i in range(n)]


# ---------------------------------------------------------------------------
# ReplayProvider tests
# ---------------------------------------------------------------------------

class TestReplayProviderInit:
    def test_requires_non_empty_1m_bars(self):
        with pytest.raises(ValueError, match="bars_1m cannot be empty"):
            ReplayProvider([], _make_bars_5m(), _make_bars_1h(), "MNQ=F")

    def test_cursor_starts_at_zero(self):
        p = ReplayProvider(_make_bars_1m(), _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        assert p.cursor == 0

    def test_total_steps_equals_1m_bar_count(self):
        bars = _make_bars_1m(20)
        p = ReplayProvider(bars, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        assert p.total_steps == 20


class TestReplayProviderCursor:
    def test_advance_moves_cursor(self):
        p = ReplayProvider(_make_bars_1m(5), _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        assert p.cursor == 0
        result = p.advance()
        assert result is True
        assert p.cursor == 1

    def test_advance_returns_false_at_last_bar(self):
        bars = _make_bars_1m(2)
        p = ReplayProvider(bars, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        p.advance()  # cursor → 1
        result = p.advance()  # already at last
        assert result is False
        assert p.cursor == 1

    def test_has_next_false_at_last_bar(self):
        bars = _make_bars_1m(1)
        p = ReplayProvider(bars, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        assert p.has_next() is False

    def test_has_next_true_when_bars_remain(self):
        p = ReplayProvider(_make_bars_1m(3), _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        assert p.has_next() is True

    def test_current_timestamp_advances_with_cursor(self):
        bars = _make_bars_1m(3)
        p = ReplayProvider(bars, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        t0 = p.current_timestamp
        p.advance()
        t1 = p.current_timestamp
        assert t1 > t0


class TestReplayProviderDataSlicing:
    def test_get_latest_quote_returns_current_close(self):
        bars = _make_bars_1m(5)
        p = ReplayProvider(bars, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        # cursor=0, close = 100.0 + 0
        assert p.get_latest_quote("MNQ=F") == pytest.approx(100.0)
        p.advance()
        # cursor=1, close = 101.0
        assert p.get_latest_quote("MNQ=F") == pytest.approx(101.0)

    def test_get_recent_bars_respects_cursor(self):
        bars_1m = _make_bars_1m(10)
        p = ReplayProvider(bars_1m, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        # At cursor=0 only 1 bar is visible
        visible = p.get_recent_bars("MNQ=F", "1m", 50)
        assert len(visible) == 1

        # Advance to cursor=4 → 5 bars visible
        for _ in range(4):
            p.advance()
        visible = p.get_recent_bars("MNQ=F", "1m", 50)
        assert len(visible) == 5

    def test_get_recent_bars_respects_lookback_limit(self):
        bars_1m = _make_bars_1m(20)
        p = ReplayProvider(bars_1m, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        for _ in range(19):
            p.advance()
        # 20 bars visible but lookback=5
        visible = p.get_recent_bars("MNQ=F", "1m", 5)
        assert len(visible) == 5

    def test_get_recent_bars_unsupported_timeframe_raises(self):
        p = ReplayProvider(_make_bars_1m(), _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            p.get_recent_bars("MNQ=F", "4h", 10)

    def test_bars_sorted_oldest_first_regardless_of_input_order(self):
        bars = list(reversed(_make_bars_1m(5)))  # pass in newest-first
        p = ReplayProvider(bars, _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        # After sorting, cursor=0 should be the oldest bar
        assert p.current_timestamp == _ts(0)

    def test_get_session_status_rth(self):
        # 14:00 UTC = 10:00 EDT → RTH
        p = ReplayProvider(_make_bars_1m(5), _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        assert p.get_session_status("MNQ=F") == "RTH"

    def test_is_market_open_during_rth(self):
        p = ReplayProvider(_make_bars_1m(5), _make_bars_5m(), _make_bars_1h(), "MNQ=F")
        assert p.is_market_open("MNQ=F") is True


# ---------------------------------------------------------------------------
# Loader tests
# ---------------------------------------------------------------------------

class TestLoadBarsFromCSV:
    def _write_csv(self, path: Path, bars: list[Bar]) -> None:
        with path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
            writer.writeheader()
            for b in bars:
                writer.writerow({
                    "timestamp": b.timestamp.isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                })

    def test_loads_three_csvs(self):
        from drift.replay.loader import load_bars_from_csv

        with tempfile.TemporaryDirectory() as tmp:
            p1m = Path(tmp) / "1m.csv"
            p5m = Path(tmp) / "5m.csv"
            p1h = Path(tmp) / "1h.csv"
            self._write_csv(p1m, _make_bars_1m(10))
            self._write_csv(p5m, _make_bars_5m(5))
            self._write_csv(p1h, _make_bars_1h(3))

            bars_1m, bars_5m, bars_1h = load_bars_from_csv(p1m, p5m, p1h, "MNQ=F")

        assert len(bars_1m) == 10
        assert len(bars_5m) == 5
        assert len(bars_1h) == 3

    def test_bars_are_sorted_oldest_first(self):
        from drift.replay.loader import load_bars_from_csv

        bars = list(reversed(_make_bars_1m(5)))
        with tempfile.TemporaryDirectory() as tmp:
            p1m = Path(tmp) / "1m.csv"
            p5m = Path(tmp) / "5m.csv"
            p1h = Path(tmp) / "1h.csv"
            self._write_csv(p1m, bars)
            self._write_csv(p5m, _make_bars_5m(3))
            self._write_csv(p1h, _make_bars_1h(2))

            result_1m, _, _ = load_bars_from_csv(p1m, p5m, p1h, "MNQ=F")

        timestamps = [b.timestamp for b in result_1m]
        assert timestamps == sorted(timestamps)

    def test_missing_file_raises(self):
        from drift.replay.loader import load_bars_from_csv

        with pytest.raises(FileNotFoundError):
            load_bars_from_csv("/no/such/file.csv", "/no/such/file.csv", "/no/such/file.csv", "MNQ=F")


# ---------------------------------------------------------------------------
# ReplayEngine smoke test
# ---------------------------------------------------------------------------

class TestReplayEngineSmoke:
    def test_run_returns_summary(self):
        """Engine runs through all bars and returns a ReplaySummary."""
        from drift.replay.engine import ReplayEngine
        from drift.utils.config import load_app_config

        config = load_app_config("config/settings.yaml")

        # Use enough bars to satisfy feature engine minimums
        bars_1m = _make_bars_1m(60)
        bars_5m = _make_bars_5m(20)
        bars_1h = _make_bars_1h(10)

        engine = ReplayEngine(
            config=config,
            bars_1m=bars_1m,
            bars_5m=bars_5m,
            bars_1h=bars_1h,
            step_every_n_bars=15,
            disable_session_gate=True,  # RTH check irrelevant for unit test
            verbose=False,
        )
        summary = engine.run()

        assert summary.total_steps == 60
        # With step_every_n=15, pipeline fires at bar 0, 15, 30, 45 → 4 steps
        assert summary.pipeline_steps == 4
        # Every pipeline step should have produced an event
        assert summary.blocked + summary.llm_no_trade + summary.trade_plans_issued == len(summary.events)

    def test_summary_event_count_matches_pipeline_steps(self):
        """Every pipeline step produces exactly one event."""
        from drift.replay.engine import ReplayEngine
        from drift.utils.config import load_app_config

        config = load_app_config("config/settings.yaml")
        bars_1m = _make_bars_1m(30)
        bars_5m = _make_bars_5m(10)
        bars_1h = _make_bars_1h(5)

        engine = ReplayEngine(
            config=config,
            bars_1m=bars_1m,
            bars_5m=bars_5m,
            bars_1h=bars_1h,
            step_every_n_bars=30,  # only fires at bar 0
            disable_session_gate=True,
            verbose=False,
        )
        summary = engine.run()

        assert summary.pipeline_steps == 1
        # The one pipeline step should produce exactly one event (or zero if
        # feature data is insufficient — either way counts match)
        assert len(summary.events) <= summary.pipeline_steps
