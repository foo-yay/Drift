"""Tests for Phase 12: live outcome resolver, watch store, and prompt watch conditions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from drift.models import Bar, TradePlan, WatchCondition
from drift.replay.outcome import OutcomeResult, resolve_outcome
from drift.storage.outcome_resolver import (
    _build_plan,
    _resolve_one,
    resolve_live_outcomes,
)
from drift.storage.signal_store import SignalStore
from drift.storage.watch_store import WatchStore

_NOW = datetime(2026, 4, 15, 14, 30, 0, tzinfo=timezone.utc)
_SYMBOL = "MNQ"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store() -> SignalStore:
    return SignalStore(":memory:")


def _make_watch_store() -> WatchStore:
    return WatchStore(":memory:")


def _make_bar(timestamp: datetime, close: float = 21000.0, *, hit_low: float | None = None, hit_high: float | None = None) -> Bar:
    low = hit_low if hit_low is not None else close - 5.0
    high = hit_high if hit_high is not None else close + 5.0
    return Bar(
        timestamp=timestamp,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
        timeframe="1m",
        symbol=_SYMBOL,
    )


def _make_signal_row(
    *,
    bias: str = "LONG",
    stop_loss: float = 20950.0,
    tp1: float = 21060.0,
    tp2: float | None = 21100.0,
    entry_min: float = 21000.0,
    entry_max: float = 21010.0,
    event_offset_minutes: int = 90,
    hold_minutes: int = 60,
):
    """Insert and return a TRADE_PLAN_ISSUED signal row."""
    from drift.models import SignalEvent
    event_time = _NOW - timedelta(minutes=event_offset_minutes)
    event = SignalEvent(
        event_time=event_time,
        symbol=_SYMBOL,
        source="live",
        final_outcome="TRADE_PLAN_ISSUED",
        final_reason=f"{bias} | pullback_continuation | confidence=75",
        trade_plan={
            "bias": bias,
            "setup_type": "pullback_continuation",
            "confidence": 75,
            "entry_min": entry_min,
            "entry_max": entry_max,
            "stop_loss": stop_loss,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "reward_risk_ratio": 2.0,
            "max_hold_minutes": hold_minutes,
        },
        llm_decision_parsed={"hold_minutes": hold_minutes},
    )
    store = _make_store()
    store.insert_event(event)
    rows = store.get_pending_live_signals(_SYMBOL)
    return store, rows[0]


# ===========================================================================
# WatchStore tests
# ===========================================================================

class TestWatchStore:
    def test_replace_watches_inserts_active(self) -> None:
        ws = _make_watch_store()
        conds = [
            WatchCondition(condition_type="price_below", value=21000.0, description="Key support", expires_minutes=60),
            WatchCondition(condition_type="rsi_below", value=35, description="Oversold reset", expires_minutes=30),
        ]
        ws.replace_watches(_SYMBOL, conds)
        active = ws.get_active(_SYMBOL)
        assert len(active) == 2

    def test_replace_watches_clears_old_ones(self) -> None:
        ws = _make_watch_store()
        ws.replace_watches(_SYMBOL, [
            WatchCondition(condition_type="price_above", value=21200.0, description="Breakout", expires_minutes=60),
        ])
        ws.replace_watches(_SYMBOL, [
            WatchCondition(condition_type="price_below", value=20900.0, description="Support", expires_minutes=60),
        ])
        active = ws.get_active(_SYMBOL)
        assert len(active) == 1
        assert active[0].condition_type == "price_below"

    def test_replace_watches_empty_clears_all(self) -> None:
        ws = _make_watch_store()
        ws.replace_watches(_SYMBOL, [
            WatchCondition(condition_type="price_above", value=21200.0, description="Breakout", expires_minutes=60),
        ])
        ws.replace_watches(_SYMBOL, [])
        assert ws.get_active(_SYMBOL) == []

    def test_mark_triggered_removes_from_active(self) -> None:
        ws = _make_watch_store()
        ws.replace_watches(_SYMBOL, [
            WatchCondition(condition_type="price_below", value=21000.0, description="Support", expires_minutes=60),
        ])
        active = ws.get_active(_SYMBOL)
        ws.mark_triggered(active[0].id)
        assert ws.get_active(_SYMBOL) == []

    def test_expired_watch_not_returned_as_active(self) -> None:
        ws = _make_watch_store()
        # expires_minutes=5 is minimum; we'll set created_at far in the past manually
        ws.replace_watches(_SYMBOL, [
            WatchCondition(condition_type="price_below", value=21000.0, description="Support", expires_minutes=5),
        ])
        # Force-expire it by direct update
        ws._conn.execute(
            "UPDATE watches SET expires_at=? WHERE symbol=?",
            ((_NOW - timedelta(minutes=1)).isoformat(), _SYMBOL),
        )
        ws._conn.commit()
        assert ws.get_active(_SYMBOL) == []

    def test_get_all_includes_triggered(self) -> None:
        ws = _make_watch_store()
        ws.replace_watches(_SYMBOL, [
            WatchCondition(condition_type="price_below", value=21000.0, description="Support", expires_minutes=60),
        ])
        active = ws.get_active(_SYMBOL)
        ws.mark_triggered(active[0].id)
        all_rows = ws.get_all(_SYMBOL)
        assert len(all_rows) == 1
        assert all_rows[0].triggered_at is not None

    def test_clear_expired_removes_old_untriggered(self) -> None:
        ws = _make_watch_store()
        ws.replace_watches(_SYMBOL, [
            WatchCondition(condition_type="price_below", value=21000.0, description="Support", expires_minutes=5),
        ])
        ws._conn.execute(
            "UPDATE watches SET expires_at=? WHERE symbol=?",
            ((_NOW - timedelta(minutes=1)).isoformat(), _SYMBOL),
        )
        ws._conn.commit()
        deleted = ws.clear_expired(_SYMBOL)
        assert deleted == 1


# ===========================================================================
# WatchCondition model tests
# ===========================================================================

class TestWatchConditionModel:
    def test_valid_price_below(self) -> None:
        w = WatchCondition(condition_type="price_below", value=21000.0, description="Support", expires_minutes=60)
        assert w.condition_type == "price_below"
        assert w.expires_minutes == 60

    def test_expires_minutes_clamped(self) -> None:
        with pytest.raises(Exception):
            WatchCondition(condition_type="price_above", value=1.0, description="x", expires_minutes=4)

    def test_llm_decision_default_empty_watches(self) -> None:
        from drift.models import LLMDecision
        d = LLMDecision(
            decision="NO_TRADE",
            confidence=0,
            setup_type="no_trade",
            thesis="no setup",
            entry_style="no_entry",
            entry_zone=[0.0, 0.0],
            invalidation_hint="n/a",
            hold_minutes=1,
            do_not_trade_if=[],
        )
        assert d.watch_conditions == []

    def test_llm_decision_with_watch_conditions(self) -> None:
        from drift.models import LLMDecision
        d = LLMDecision(
            decision="NO_TRADE",
            confidence=0,
            setup_type="no_trade",
            thesis="no setup",
            entry_style="no_entry",
            entry_zone=[0.0, 0.0],
            invalidation_hint="n/a",
            hold_minutes=1,
            do_not_trade_if=[],
            watch_conditions=[
                WatchCondition(condition_type="price_below", value=21000.0, description="Support", expires_minutes=60),
            ],
        )
        assert len(d.watch_conditions) == 1
        assert d.watch_conditions[0].condition_type == "price_below"


# ===========================================================================
# Outcome resolver tests
# ===========================================================================

class TestBuildPlan:
    def _row_with(self, **overrides):
        from drift.storage.signal_store import SignalRow
        defaults = dict(
            id=1, signal_key="abc", symbol=_SYMBOL, source="live",
            event_time_utc=_NOW.isoformat(), as_of_utc=None,
            final_outcome="TRADE_PLAN_ISSUED",
            bias="LONG", setup_type="pullback_continuation", confidence=75,
            entry_min=21000.0, entry_max=21010.0, stop_loss=20950.0,
            take_profit_1=21060.0, take_profit_2=21100.0, reward_risk=2.0,
            pnl_points=None, replay_outcome=None, thesis="test",
            snapshot_json=None, gate_report_json=None,
            llm_json='{"hold_minutes": 30}', created_at=_NOW.isoformat(),
        )
        defaults.update(overrides)
        return SignalRow(**defaults)

    def test_valid_row_returns_plan(self) -> None:
        row = self._row_with()
        plan = _build_plan(row)
        assert plan is not None
        assert plan.bias == "LONG"
        assert plan.stop_loss == 20950.0

    def test_missing_stop_returns_none(self) -> None:
        row = self._row_with(stop_loss=None)
        assert _build_plan(row) is None

    def test_missing_tp1_returns_none(self) -> None:
        row = self._row_with(take_profit_1=None)
        assert _build_plan(row) is None

    def test_hold_minutes_from_llm_json(self) -> None:
        row = self._row_with(llm_json='{"hold_minutes": 45}')
        plan = _build_plan(row)
        assert plan.max_hold_minutes == 45

    def test_hold_minutes_fallback_when_no_llm_json(self) -> None:
        row = self._row_with(llm_json=None)
        plan = _build_plan(row)
        assert plan.max_hold_minutes == 60  # default


class TestResolveOne:
    def _make_row(self, event_offset_minutes: int = 90):
        from drift.storage.signal_store import SignalRow
        # Use actual now so elapsed-minutes comparisons work correctly.
        actual_now = datetime.now(tz=timezone.utc)
        return SignalRow(
            id=1, signal_key="abc", symbol=_SYMBOL, source="live",
            event_time_utc=(actual_now - timedelta(minutes=event_offset_minutes)).isoformat(),
            as_of_utc=None, final_outcome="TRADE_PLAN_ISSUED",
            bias="LONG", setup_type="pullback_continuation", confidence=75,
            entry_min=21000.0, entry_max=21010.0, stop_loss=20950.0,
            take_profit_1=21060.0, take_profit_2=21100.0, reward_risk=2.0,
            pnl_points=None, replay_outcome=None, thesis="test",
            snapshot_json=None, gate_report_json=None,
            llm_json='{"hold_minutes": 60}', created_at=actual_now.isoformat(),
        )

    def test_resolves_tp1_hit(self) -> None:
        store = _make_store()
        row = self._make_row(event_offset_minutes=90)

        # Provide bars after the signal time: bars that hit TP1 high
        signal_time = datetime.fromisoformat(row.event_time_utc).replace(tzinfo=timezone.utc)
        bars = [_make_bar(signal_time + timedelta(minutes=i + 1), hit_high=21070.0) for i in range(5)]

        provider = MagicMock()
        provider.get_recent_bars.return_value = bars

        # The store is in-memory and row wasn't inserted — upsert is a no-op
        # but should not raise. We verify _resolve_one returns 1 (resolved).
        count = _resolve_one(store, row, provider)
        assert count == 1

    def test_within_hold_window_skips(self) -> None:
        store = _make_store()
        row = self._make_row(event_offset_minutes=10)  # 10 min elapsed, hold=60
        provider = MagicMock()
        count = _resolve_one(store, row, provider)
        assert count == 0
        provider.get_recent_bars.assert_not_called()

    def test_no_bars_after_signal_returns_zero(self) -> None:
        store = _make_store()
        row = self._make_row(event_offset_minutes=90)
        signal_time = datetime.fromisoformat(row.event_time_utc).replace(tzinfo=timezone.utc)
        provider = MagicMock()
        # All bars are before the signal time
        provider.get_recent_bars.return_value = [
            _make_bar(signal_time - timedelta(minutes=1))
        ]
        count = _resolve_one(store, row, provider)
        assert count == 0


class TestResolveLiveOutcomes:
    def test_no_pending_returns_zero(self) -> None:
        store = _make_store()
        provider = MagicMock()
        assert resolve_live_outcomes(store, _SYMBOL, provider) == 0
        provider.get_recent_bars.assert_not_called()


# ===========================================================================
# Scheduler condition helpers
# ===========================================================================

class TestSchedulerConditionHelpers:
    def test_price_above_triggers(self) -> None:
        from drift.gui.scheduler import _condition_met
        assert _condition_met("price_above", 21000.0, 21001.0, None) is True

    def test_price_above_not_triggered(self) -> None:
        from drift.gui.scheduler import _condition_met
        assert _condition_met("price_above", 21000.0, 20999.0, None) is False

    def test_price_below_triggers(self) -> None:
        from drift.gui.scheduler import _condition_met
        assert _condition_met("price_below", 21000.0, 20999.0, None) is True

    def test_price_below_not_triggered(self) -> None:
        from drift.gui.scheduler import _condition_met
        assert _condition_met("price_below", 21000.0, 21001.0, None) is False

    def test_rsi_below_triggers(self) -> None:
        from drift.gui.scheduler import _condition_met
        assert _condition_met("rsi_below", 35.0, 21000.0, 30.0) is True

    def test_rsi_above_triggers(self) -> None:
        from drift.gui.scheduler import _condition_met
        assert _condition_met("rsi_above", 70.0, 21000.0, 75.0) is True

    def test_rsi_skipped_when_none(self) -> None:
        from drift.gui.scheduler import _condition_met
        assert _condition_met("rsi_below", 35.0, 21000.0, None) is False

    def test_compute_rsi_insufficient_bars(self) -> None:
        from drift.gui.scheduler import _compute_rsi
        assert _compute_rsi([]) is None

    def test_compute_rsi_trending_up_gives_high_value(self) -> None:
        from drift.gui.scheduler import _compute_rsi
        from datetime import datetime, timezone
        bars = [
            _make_bar(datetime(2026, 4, 15, 14, i, tzinfo=timezone.utc), close=21000.0 + i * 5)
            for i in range(20)
        ]
        rsi = _compute_rsi(bars)
        assert rsi is not None
        assert rsi > 60  # strongly trending up → high RSI


# ===========================================================================
# Prompt builder — watch conditions appear in system prompt
# ===========================================================================

class TestPromptBuilderWatchConditions:
    def test_system_prompt_contains_watch_conditions_schema(self) -> None:
        from drift.ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        assert "watch_conditions" in pb.system_prompt

    def test_system_prompt_contains_condition_types(self) -> None:
        from drift.ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        for ct in ("price_above", "price_below", "rsi_above", "rsi_below"):
            assert ct in pb.system_prompt

    def test_system_prompt_instructs_on_no_trade(self) -> None:
        from drift.ai.prompt_builder import PromptBuilder
        pb = PromptBuilder()
        assert "NO_TRADE" in pb.system_prompt
        assert "watch_conditions" in pb.system_prompt
