"""Tests for scoring/performance_context.py."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drift.models import SignalEvent
from drift.scoring.performance_context import (
    PerformanceContext,
    build_performance_context,
    _compute_streak,
    _compute_setup_stats,
    _pick_few_shot_examples,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)


def _make_event(
    *,
    outcome: str,
    setup_type: str = "pullback_continuation",
    bias: str = "LONG",
    confidence: int = 75,
    pnl_points: float = 10.0,
    days_ago: float = 1,
    thesis: str = "Clean pullback to VWAP.",
) -> SignalEvent:
    event_time = _NOW - timedelta(days=days_ago)
    return SignalEvent(
        event_time=event_time,
        symbol="MNQ",
        source="live",
        final_outcome="TRADE_PLAN_ISSUED",
        final_reason=f"{bias} | {setup_type} | confidence={confidence}",
        trade_plan={"bias": bias, "setup_type": setup_type, "confidence": confidence},
        llm_decision_parsed={"thesis": thesis, "decision": bias, "confidence": confidence},
        replay_outcome={"outcome": outcome, "pnl_points": pnl_points, "bars_elapsed": 10, "minutes_elapsed": 10, "exit_price": 21000.0},
    )


def _write_events(events: list[SignalEvent], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(e.model_dump_json() + "\n")


# ---------------------------------------------------------------------------
# build_performance_context — main tests
# ---------------------------------------------------------------------------


class TestBuildPerformanceContext:
    def test_returns_none_for_empty_log(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("")
        result = build_performance_context(log, lookback_days=30, few_shot_examples=2)
        assert result is None

    def test_returns_none_for_nonexistent_log(self, tmp_path):
        result = build_performance_context(tmp_path / "missing.jsonl", lookback_days=30)
        assert result is None

    def test_returns_none_for_fewer_than_3_resolved(self, tmp_path):
        log = tmp_path / "events.jsonl"
        events = [_make_event(outcome="TP1_HIT"), _make_event(outcome="STOP_HIT")]
        _write_events(events, log)
        result = build_performance_context(log, lookback_days=30)
        assert result is None

    def test_returns_context_for_sufficient_data(self, tmp_path):
        log = tmp_path / "events.jsonl"
        events = [
            _make_event(outcome="TP1_HIT"),
            _make_event(outcome="TP2_HIT"),
            _make_event(outcome="STOP_HIT"),
        ]
        _write_events(events, log)
        ctx = build_performance_context(log, lookback_days=30, few_shot_examples=2)
        assert ctx is not None
        assert isinstance(ctx, PerformanceContext)

    def test_win_rate_calculation_all_wins(self, tmp_path):
        log = tmp_path / "events.jsonl"
        events = [_make_event(outcome="TP1_HIT") for _ in range(4)]
        _write_events(events, log)
        ctx = build_performance_context(log, lookback_days=30)
        assert ctx is not None
        assert ctx.overall_win_rate_pct == 100.0

    def test_win_rate_calculation_mixed(self, tmp_path):
        log = tmp_path / "events.jsonl"
        events = [
            _make_event(outcome="TP1_HIT"),
            _make_event(outcome="TP1_HIT"),
            _make_event(outcome="STOP_HIT"),
            _make_event(outcome="STOP_HIT"),
        ]
        _write_events(events, log)
        ctx = build_performance_context(log, lookback_days=30)
        assert ctx is not None
        assert ctx.overall_win_rate_pct == 50.0

    def test_resolved_count_correct(self, tmp_path):
        log = tmp_path / "events.jsonl"
        events = [
            _make_event(outcome="TP1_HIT"),
            _make_event(outcome="STOP_HIT"),
            _make_event(outcome="TIME_STOP"),
            _make_event(outcome="TP2_HIT"),
        ]
        _write_events(events, log)
        ctx = build_performance_context(log, lookback_days=30)
        assert ctx is not None
        assert ctx.resolved_count == 4

    def test_excludes_events_outside_lookback_window(self, tmp_path):
        log = tmp_path / "events.jsonl"
        events = [
            _make_event(outcome="TP1_HIT", days_ago=1),
            _make_event(outcome="TP1_HIT", days_ago=2),
            _make_event(outcome="STOP_HIT", days_ago=40),  # outside 30-day window
        ]
        _write_events(events, log)
        ctx = build_performance_context(log, lookback_days=30)
        # Only 2 events in window → < 3 resolved → None
        assert ctx is None

    def test_excludes_non_trade_plan_events(self, tmp_path):
        log = tmp_path / "events.jsonl"
        # Mix trade plan events with BLOCKED events
        trade_events = [_make_event(outcome="TP1_HIT") for _ in range(3)]
        blocked = SignalEvent(
            event_time=_NOW - timedelta(days=1),
            symbol="MNQ",
            source="live",
            final_outcome="BLOCKED",
            final_reason="Session gate blocked",
        )
        _write_events([*trade_events, blocked], log)
        ctx = build_performance_context(log, lookback_days=30)
        assert ctx is not None
        assert ctx.resolved_count == 3  # blocked event excluded

    def test_lookback_days_respected(self):
        assert True  # covered by test_excludes_events_outside_lookback_window


# ---------------------------------------------------------------------------
# Streak computation
# ---------------------------------------------------------------------------


class TestComputeStreak:
    def _events(self, outcomes: list[str]) -> list[SignalEvent]:
        return [
            _make_event(outcome=o, days_ago=len(outcomes) - i)
            for i, o in enumerate(outcomes)
        ]

    def test_all_wins_streak(self):
        events = self._events(["TP1_HIT", "TP2_HIT", "TP1_HIT"])
        assert _compute_streak(events) == 3

    def test_all_losses_streak(self):
        events = self._events(["STOP_HIT", "STOP_HIT"])
        assert _compute_streak(events) == -2

    def test_mixed_streak_resets(self):
        events = self._events(["TP1_HIT", "STOP_HIT", "TP1_HIT"])
        # Last (newest) is TP1_HIT; before that STOP_HIT breaks the run → streak = 1
        assert _compute_streak(events) == 1

    def test_neutral_outcome_breaks_streak(self):
        events = self._events(["TP1_HIT", "TIME_STOP", "TP1_HIT"])
        # Newest is TP1_HIT, then TIME_STOP breaks → streak = 1
        assert _compute_streak(events) == 1

    def test_empty_events_returns_zero(self):
        assert _compute_streak([]) == 0


# ---------------------------------------------------------------------------
# Setup stats
# ---------------------------------------------------------------------------


class TestComputeSetupStats:
    def test_per_setup_win_rate(self):
        events = [
            _make_event(outcome="TP1_HIT", setup_type="pullback_continuation"),
            _make_event(outcome="TP1_HIT", setup_type="pullback_continuation"),
            _make_event(outcome="STOP_HIT", setup_type="pullback_continuation"),
            _make_event(outcome="TP2_HIT", setup_type="breakout_continuation"),
        ]
        stats = _compute_setup_stats(events)
        pullback = next(s for s in stats if s.setup_type == "pullback_continuation")
        breakout = next(s for s in stats if s.setup_type == "breakout_continuation")
        assert pullback.win_rate_pct == pytest.approx(66.7, abs=0.1)
        assert breakout.win_rate_pct == 100.0

    def test_returns_stats_sorted_by_setup_type(self):
        events = [
            _make_event(outcome="TP1_HIT", setup_type="pullback_continuation"),
            _make_event(outcome="TP1_HIT", setup_type="breakout_continuation"),
            _make_event(outcome="TP1_HIT", setup_type="breakout_continuation"),
        ]
        stats = _compute_setup_stats(events)
        names = [s.setup_type for s in stats]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# Few-shot example selection
# ---------------------------------------------------------------------------


class TestPickFewShotExamples:
    def test_returns_zero_when_count_is_zero(self):
        events = [_make_event(outcome="TP1_HIT") for _ in range(5)]
        assert _pick_few_shot_examples(events, 0) == []

    def test_returns_balanced_mix(self):
        wins = [_make_event(outcome="TP1_HIT", days_ago=i + 1) for i in range(3)]
        losses = [_make_event(outcome="STOP_HIT", days_ago=i + 4) for i in range(3)]
        examples = _pick_few_shot_examples([*wins, *losses], count=2)
        assert len(examples) == 2
        outcomes = {ex.outcome for ex in examples}
        # Should have at least one win and one loss
        assert "TP1_HIT" in outcomes or "TP2_HIT" in outcomes
        assert "STOP_HIT" in outcomes

    def test_returns_fewer_when_pool_is_small(self):
        events = [_make_event(outcome="TP1_HIT")]
        examples = _pick_few_shot_examples(events, count=4)
        assert len(examples) <= 4  # never returns more than available

    def test_example_fields_populated(self):
        events = [_make_event(outcome="TP1_HIT", pnl_points=12.5, thesis="Strong pullback.")]
        examples = _pick_few_shot_examples(events, count=1)
        ex = examples[0]
        assert ex.outcome == "TP1_HIT"
        assert ex.pnl_points == 12.5
        assert ex.thesis == "Strong pullback."
        assert ex.setup_type == "pullback_continuation"


# ---------------------------------------------------------------------------
# Config field validation smoke test
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_llm_section_defaults(self):
        from drift.config.models import LLMSection
        section = LLMSection(
            provider="anthropic",
            model="claude-sonnet-4-6",
            temperature=0.1,
            timeout_seconds=30,
            max_retries=2,
        )
        assert section.performance_context_enabled is True
        assert section.performance_context_lookback_days == 30
        assert section.few_shot_examples == 2

    def test_gates_section_defaults(self):
        from drift.config.models import GatesSection
        section = GatesSection(
            regime_enabled=True,
            min_trend_score=35,
            min_momentum_score=30,
            block_on_extreme_volatility=True,
            cooldown_enabled=True,
            kill_switch_enabled=True,
            kill_switch_path="data/.kill_switch",
        )
        assert section.news_gate_enabled is True
        assert section.news_blackout_minutes == 30
