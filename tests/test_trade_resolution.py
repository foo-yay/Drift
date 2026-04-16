"""Tests for live trade-plan resolution helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from drift.gui.scheduler import (
    _classify_resolved_outcome,
    _entry_zone_in_bar_range,
)
from drift.models import Bar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)

# Minimal plan-like object (uses SimpleNamespace so we don't need a real SignalRow)
def _plan(
    bias: str = "LONG",
    entry_min: float = 100.0,
    entry_max: float = 110.0,
    stop_loss: float = 90.0,
    take_profit_1: float = 130.0,
    take_profit_2: float = 150.0,
    issued_minutes_ago: float = 5.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        bias=bias,
        entry_min=entry_min,
        entry_max=entry_max,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        event_time=_NOW - timedelta(minutes=issued_minutes_ago),
    )


def _bar(
    high: float,
    low: float,
    minutes_ago: float = 1.0,
    symbol: str = "MNQ",
) -> Bar:
    ts = _NOW - timedelta(minutes=minutes_ago)
    mid = (high + low) / 2
    return Bar(
        timestamp=ts,
        open=mid,
        high=high,
        low=low,
        close=mid,
        volume=100.0,
        timeframe="1m",
        symbol=symbol,
    )


# ---------------------------------------------------------------------------
# _entry_zone_in_bar_range
# ---------------------------------------------------------------------------

class TestEntryZoneInBarRange:
    def test_bar_fully_inside_zone(self):
        """Bar entirely within [100, 110] → touched."""
        bars = [_bar(high=108, low=103)]
        assert _entry_zone_in_bar_range(_plan(), bars) is True

    def test_bar_overlaps_zone_from_below(self):
        """Bar reaches up into the zone from below."""
        bars = [_bar(high=105, low=90)]
        assert _entry_zone_in_bar_range(_plan(), bars) is True

    def test_bar_overlaps_zone_from_above(self):
        """Bar reaches down into the zone from above."""
        bars = [_bar(high=120, low=108)]
        assert _entry_zone_in_bar_range(_plan(), bars) is True

    def test_bar_completely_above_zone(self):
        bars = [_bar(high=160, low=115)]
        assert _entry_zone_in_bar_range(_plan(), bars) is False

    def test_bar_completely_below_zone(self):
        bars = [_bar(high=98, low=80)]
        assert _entry_zone_in_bar_range(_plan(), bars) is False

    def test_bar_touches_entry_max_exactly(self):
        """Bar high exactly equals entry_max → should count as touched."""
        bars = [_bar(high=110, low=95)]
        assert _entry_zone_in_bar_range(_plan(), bars) is True

    def test_bar_touches_entry_min_exactly(self):
        """Bar low exactly equals entry_min → should count as touched."""
        bars = [_bar(high=120, low=100)]
        assert _entry_zone_in_bar_range(_plan(), bars) is True

    def test_bar_before_plan_issuance_ignored(self):
        """Bars with timestamps before the plan was issued must be excluded."""
        # Bar is 10 minutes old; plan was issued 5 minutes ago → bar predates plan.
        early_bar = _bar(high=108, low=103, minutes_ago=10)
        assert _entry_zone_in_bar_range(_plan(issued_minutes_ago=5), [early_bar]) is False

    def test_bar_after_plan_issuance_included(self):
        """Bars newer than plan issuance are included."""
        recent_bar = _bar(high=108, low=103, minutes_ago=2)
        assert _entry_zone_in_bar_range(_plan(issued_minutes_ago=5), [recent_bar]) is True

    def test_returns_true_when_any_bar_touches(self):
        """Only one of many bars needs to overlap the zone."""
        bars = [
            _bar(high=95, low=80, minutes_ago=4),   # misses
            _bar(high=160, low=130, minutes_ago=3),  # misses
            _bar(high=108, low=103, minutes_ago=2),  # hits!
        ]
        assert _entry_zone_in_bar_range(_plan(issued_minutes_ago=5), bars) is True

    def test_empty_bars_returns_false(self):
        assert _entry_zone_in_bar_range(_plan(), []) is False

    def test_missing_entry_levels_returns_false(self):
        """Plan with None entry levels cannot be checked."""
        plan = SimpleNamespace(entry_min=None, entry_max=None, event_time=_NOW)
        assert _entry_zone_in_bar_range(plan, [_bar(high=108, low=103)]) is False


# ---------------------------------------------------------------------------
# _classify_resolved_outcome — LONG
# ---------------------------------------------------------------------------

class TestClassifyResolvedOutcomeLong:
    def test_entry_not_touched_returns_entry_missed(self):
        outcome, pnl = _classify_resolved_outcome(_plan(), price=130.0, zone_touched=False)
        assert outcome == "ENTRY_MISSED"
        assert pnl == 0.0

    def test_tp1_hit_long(self):
        p = _plan(entry_min=100, entry_max=110, take_profit_1=130)
        outcome, pnl = _classify_resolved_outcome(p, price=130.0, zone_touched=True)
        assert outcome == "TP1_HIT"
        # entry_mid = 105; pnl = 130 - 105 = 25
        assert pnl == 25.0

    def test_tp2_hit_long_takes_priority_over_tp1(self):
        """If price is at TP2, should classify as TP2_HIT not TP1_HIT (TP2 > TP1)."""
        p = _plan(entry_min=100, entry_max=110, take_profit_1=130, take_profit_2=150)
        outcome, pnl = _classify_resolved_outcome(p, price=152.0, zone_touched=True)
        assert outcome == "TP2_HIT"
        assert pnl == 45.0  # 150 - 105

    def test_stop_hit_long(self):
        p = _plan(entry_min=100, entry_max=110, stop_loss=90)
        outcome, pnl = _classify_resolved_outcome(p, price=89.0, zone_touched=True)
        assert outcome == "STOP_HIT"
        assert pnl == -15.0  # 90 - 105

    def test_stop_exactly_at_stop_loss(self):
        p = _plan(entry_min=100, entry_max=110, stop_loss=90)
        outcome, pnl = _classify_resolved_outcome(p, price=90.0, zone_touched=True)
        assert outcome == "STOP_HIT"


# ---------------------------------------------------------------------------
# _classify_resolved_outcome — SHORT
# ---------------------------------------------------------------------------

class TestClassifyResolvedOutcomeShort:
    def test_tp1_hit_short(self):
        p = _plan(
            bias="SHORT",
            entry_min=100,
            entry_max=110,
            stop_loss=120,
            take_profit_1=80,
            take_profit_2=60,
        )
        outcome, pnl = _classify_resolved_outcome(p, price=79.0, zone_touched=True)
        assert outcome == "TP1_HIT"
        # entry_mid = 105; pnl = 105 - 80 = 25
        assert pnl == 25.0

    def test_tp2_hit_short(self):
        p = _plan(
            bias="SHORT",
            entry_min=100,
            entry_max=110,
            stop_loss=120,
            take_profit_1=80,
            take_profit_2=60,
        )
        outcome, pnl = _classify_resolved_outcome(p, price=58.0, zone_touched=True)
        assert outcome == "TP2_HIT"
        assert pnl == 45.0  # 105 - 60

    def test_stop_hit_short(self):
        p = _plan(
            bias="SHORT",
            entry_min=100,
            entry_max=110,
            stop_loss=120,
            take_profit_1=80,
            take_profit_2=None,  # no TP2 — isolates the stop path
        )
        outcome, pnl = _classify_resolved_outcome(p, price=121.0, zone_touched=True)
        assert outcome == "STOP_HIT"
        assert pnl == -15.0  # 105 - 120

    def test_entry_not_touched_short(self):
        p = _plan(bias="SHORT")
        outcome, pnl = _classify_resolved_outcome(p, price=75.0, zone_touched=False)
        assert outcome == "ENTRY_MISSED"
        assert pnl == 0.0


# ---------------------------------------------------------------------------
# SignalStore.resolve_live_signal
# ---------------------------------------------------------------------------

class TestResolveLiveSignal:
    def test_writes_outcome_by_id(self):
        from datetime import date
        from drift.models import SignalEvent
        from drift.storage.signal_store import SignalStore

        store = SignalStore(":memory:")
        event = SignalEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol="MNQ",
            source="live",
            snapshot={"as_of": datetime.now(tz=timezone.utc).isoformat()},
            final_outcome="TRADE_PLAN_ISSUED",
            final_reason="test",
            trade_plan={
                "bias": "LONG",
                "entry_min": 100.0,
                "entry_max": 110.0,
                "stop_loss": 90.0,
                "take_profit_1": 130.0,
                "take_profit_2": 150.0,
                "max_hold_minutes": 15,
            },
        )
        store.insert_event(event)
        rows = store.get_pending_live_signals("MNQ")
        assert len(rows) == 1
        signal_id = rows[0].id

        store.resolve_live_signal(signal_id, "TP1_HIT", 25.0)

        after = store.get_pending_live_signals("MNQ")
        assert after == []  # no longer pending (replay_outcome is set)

        resolved = store.query(symbol="MNQ", trade_plans_only=True)
        assert resolved[0].replay_outcome == "TP1_HIT"
        assert resolved[0].pnl_points == 25.0

    def test_entry_missed_pnl_zero(self):
        from drift.models import SignalEvent
        from drift.storage.signal_store import SignalStore

        store = SignalStore(":memory:")
        event = SignalEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol="MNQ",
            source="live",
            snapshot={"as_of": datetime.now(tz=timezone.utc).isoformat()},
            final_outcome="TRADE_PLAN_ISSUED",
            final_reason="test",
        )
        store.insert_event(event)
        row = store.get_pending_live_signals("MNQ")[0]

        store.resolve_live_signal(row.id, "ENTRY_MISSED", 0.0)
        result = store.query(symbol="MNQ", trade_plans_only=True)[0]
        assert result.replay_outcome == "ENTRY_MISSED"
        assert result.pnl_points == 0.0
