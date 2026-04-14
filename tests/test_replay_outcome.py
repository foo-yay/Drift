"""Tests for replay/outcome.py — outcome resolver logic."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from drift.models import TradePlan
from drift.replay.outcome import resolve_outcome


def _bar(low: float, high: float, close: float | None = None):
    """Build a minimal Bar for testing."""
    from drift.models import Bar
    return Bar(
        timestamp=datetime(2026, 4, 13, 10, 0, tzinfo=timezone.utc),
        open=low,
        high=high,
        low=low,
        close=close if close is not None else (low + high) / 2,
        volume=1000,
        timeframe="1m",
        symbol="MNQ",
    )


def _long_plan(**overrides) -> TradePlan:
    defaults = dict(
        generated_at=datetime(2026, 4, 13, 9, 45, tzinfo=timezone.utc),
        symbol="MNQ",
        bias="LONG",
        setup_type="pullback_continuation",
        confidence=74,
        entry_min=21_000.0,
        entry_max=21_010.0,
        stop_loss=20_985.0,
        take_profit_1=21_028.0,
        take_profit_2=21_046.0,
        reward_risk_ratio=1.8,
        max_hold_minutes=20,
        thesis="Test",
        invalidation_conditions=[],
        operator_instructions=[],
        do_not_trade_if=[],
    )
    defaults.update(overrides)
    return TradePlan(**defaults)


def _short_plan(**overrides) -> TradePlan:
    defaults = dict(
        generated_at=datetime(2026, 4, 13, 9, 45, tzinfo=timezone.utc),
        symbol="MNQ",
        bias="SHORT",
        setup_type="breakdown",
        confidence=70,
        entry_min=21_000.0,
        entry_max=21_010.0,
        stop_loss=21_025.0,
        take_profit_1=20_982.0,
        take_profit_2=20_964.0,
        reward_risk_ratio=1.8,
        max_hold_minutes=20,
        thesis="Test",
        invalidation_conditions=[],
        operator_instructions=[],
        do_not_trade_if=[],
    )
    defaults.update(overrides)
    return TradePlan(**defaults)


# ------------------------------------------------------------------
# LONG outcomes
# ------------------------------------------------------------------

class TestLongOutcomes:
    def test_tp1_hit(self):
        plan = _long_plan()
        bars = [_bar(21_000, 21_028)]
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TP1_HIT"
        assert result.bars_elapsed == 1
        assert result.exit_price == 21_028.0
        assert result.pnl_points > 0

    def test_tp2_hit(self):
        plan = _long_plan()
        bars = [_bar(21_000, 21_050)]
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TP2_HIT"
        assert result.exit_price == 21_046.0

    def test_stop_hit(self):
        plan = _long_plan()
        bars = [_bar(20_980, 21_010)]
        result = resolve_outcome(plan, bars)
        assert result.outcome == "STOP_HIT"
        assert result.exit_price == 20_985.0
        assert result.pnl_points < 0

    def test_stop_wins_same_bar_ambiguity(self):
        """If stop and TP1 are both in the same bar, stop takes priority."""
        plan = _long_plan()
        bars = [_bar(20_980, 21_030)]  # both stop and TP1 touched
        result = resolve_outcome(plan, bars)
        assert result.outcome == "STOP_HIT"

    def test_tp1_hit_after_several_bars(self):
        plan = _long_plan()
        bars = [
            _bar(21_000, 21_015),
            _bar(21_005, 21_020),
            _bar(21_010, 21_030),  # TP1 hit on bar 3
        ]
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TP1_HIT"
        assert result.bars_elapsed == 3

    def test_time_stop(self):
        plan = _long_plan(max_hold_minutes=3)
        bars = [_bar(21_000, 21_015, close=21_012)] * 3  # never hits target or stop
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TIME_STOP"
        assert result.bars_elapsed == 3

    def test_session_end_before_time_stop(self):
        plan = _long_plan(max_hold_minutes=20)
        bars = [_bar(21_000, 21_015, close=21_012)] * 5  # only 5 bars, session ended
        result = resolve_outcome(plan, bars)
        assert result.outcome == "SESSION_END"
        assert result.bars_elapsed == 5

    def test_no_tp2_falls_through_to_tp1(self):
        plan = _long_plan(take_profit_2=None)
        bars = [_bar(21_000, 21_050)]  # high above both TP levels
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TP1_HIT"

    def test_empty_bars_session_end(self):
        plan = _long_plan(max_hold_minutes=20)
        result = resolve_outcome(plan, [])
        assert result.outcome == "SESSION_END"
        assert result.bars_elapsed == 0


# ------------------------------------------------------------------
# SHORT outcomes
# ------------------------------------------------------------------

class TestShortOutcomes:
    def test_tp1_hit(self):
        plan = _short_plan()
        bars = [_bar(20_982, 21_010)]
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TP1_HIT"
        assert result.exit_price == 20_982.0
        assert result.pnl_points > 0

    def test_tp2_hit(self):
        plan = _short_plan()
        bars = [_bar(20_960, 21_000)]
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TP2_HIT"
        assert result.exit_price == 20_964.0

    def test_stop_hit(self):
        plan = _short_plan()
        bars = [_bar(21_010, 21_030)]
        result = resolve_outcome(plan, bars)
        assert result.outcome == "STOP_HIT"
        assert result.exit_price == 21_025.0
        assert result.pnl_points < 0

    def test_stop_wins_same_bar_ambiguity(self):
        plan = _short_plan()
        bars = [_bar(20_978, 21_030)]  # both stop and TP1 touched
        result = resolve_outcome(plan, bars)
        assert result.outcome == "STOP_HIT"

    def test_time_stop(self):
        plan = _short_plan(max_hold_minutes=2)
        bars = [_bar(20_990, 21_015, close=21_003)] * 2
        result = resolve_outcome(plan, bars)
        assert result.outcome == "TIME_STOP"
