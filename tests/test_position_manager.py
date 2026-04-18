"""Tests for PositionManager validation logic (no IB connection needed)."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from drift.brokers.position_manager import PositionManager
from drift.storage.trade_store import TradeStore


# ------------------------------------------------------------------
# Minimal stubs
# ------------------------------------------------------------------

@dataclass
class _BrokerCfg:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 1
    account: str = "DU123"
    order_timeout_seconds: int = 30
    approval_expiry_minutes: int = 15
    auto_start_gateway: bool = False
    gateway_script: str = ""


@dataclass
class _AppCfg:
    broker: _BrokerCfg = field(default_factory=_BrokerCfg)


@dataclass
class _Order:
    id: int = 1
    signal_key: str = "MNQ:LONG:pullback_continuation:1"
    symbol: str = "MNQ"
    bias: str = "LONG"
    setup_type: str = "pullback_continuation"
    entry_min: float = 18_990.0
    entry_max: float = 19_010.0
    stop_loss: float = 18_975.0
    take_profit_1: float = 19_040.0
    take_profit_2: float = 19_070.0
    max_hold_minutes: int = 30
    thesis: str = "Test thesis"
    generated_at: str = ""  # set per-test


def _now_iso(offset_minutes: float = 0) -> str:
    t = datetime.now(tz=timezone.utc) + timedelta(minutes=offset_minutes)
    return t.isoformat()


@pytest.fixture()
def manager(tmp_path):
    cfg = _AppCfg()
    return PositionManager(cfg, tmp_path / "test.db")


# ------------------------------------------------------------------
# validate_for_approval — time horizon
# ------------------------------------------------------------------

def test_no_errors_for_fresh_order(manager):
    order = _Order(generated_at=_now_iso())
    errors = manager.validate_for_approval(order)
    assert errors == []


def test_expired_order_blocked(manager):
    order = _Order(generated_at=_now_iso(offset_minutes=-35), max_hold_minutes=30)
    errors = manager.validate_for_approval(order)
    assert any("expired" in e.lower() for e in errors)


def test_empty_generated_at_skips_time_check(manager):
    order = _Order(generated_at="")
    errors = manager.validate_for_approval(order)
    assert errors == []


# ------------------------------------------------------------------
# validate_for_approval — duplicate position guard
# ------------------------------------------------------------------

def test_duplicate_guard_blocks_second_order(manager, tmp_path):
    # Seed a WORKING trade directly in the store
    store = TradeStore(tmp_path / "test.db")
    store.create(
        signal_key="MNQ:LONG:breakout:99",
        symbol="MNQ",
        bias="LONG",
        setup_type="breakout_continuation",
        entry_min=19_000.0,
        entry_max=19_000.0,
        stop_loss=18_980.0,
        take_profit_1=19_030.0,
        take_profit_2=19_060.0,
        max_hold_minutes=30,
        thesis="existing",
        state="WORKING",
        entry_limit=19_000.0,
    )
    order = _Order(generated_at=_now_iso())
    errors = manager.validate_for_approval(order)
    assert any("already open" in e.lower() for e in errors)


def test_no_duplicate_guard_when_no_positions(manager):
    order = _Order(generated_at=_now_iso())
    errors = manager.validate_for_approval(order)
    assert not any("already open" in e.lower() for e in errors)


# ------------------------------------------------------------------
# check_price_validity
# ------------------------------------------------------------------

def test_price_in_zone_no_warnings(manager):
    order = _Order()
    warnings = manager.check_price_validity(order, current_price=19_000.0)
    assert warnings == []


def test_price_above_zone_warns(manager):
    order = _Order()
    warnings = manager.check_price_validity(order, current_price=19_100.0)
    assert len(warnings) == 1
    assert "outside entry zone" in warnings[0]


def test_price_below_zone_warns(manager):
    order = _Order()
    warnings = manager.check_price_validity(order, current_price=18_900.0)
    assert len(warnings) == 1
    assert "outside entry zone" in warnings[0]


def test_price_at_entry_min_boundary_ok(manager):
    order = _Order()
    warnings = manager.check_price_validity(order, current_price=order.entry_min)
    assert warnings == []


def test_price_at_entry_max_boundary_ok(manager):
    order = _Order()
    warnings = manager.check_price_validity(order, current_price=order.entry_max)
    assert warnings == []


# ------------------------------------------------------------------
# apply_assessment — hold (no-op)
# ------------------------------------------------------------------

def _seed_filled_trade(tmp_path, manager):
    """Seed a FILLED trade and return its id."""
    store = TradeStore(tmp_path / "test.db")
    tid = store.create(
        signal_key="MNQ:LONG:pullback:assess1",
        symbol="MNQ",
        bias="LONG",
        setup_type="pullback_continuation",
        entry_min=19_000.0,
        entry_max=19_010.0,
        stop_loss=18_975.0,
        take_profit_1=19_040.0,
        take_profit_2=19_070.0,
        max_hold_minutes=60,
        thesis="test",
        state="FILLED",
        entry_limit=19_005.0,
        parent_order_id=100,
        tp_order_id=101,
        sl_order_id=102,
    )
    store.mark_filled(tid, 19_005.0)
    return tid


def test_apply_hold_no_changes(manager, tmp_path):
    from drift.models import AssessmentRecommendation

    tid = _seed_filled_trade(tmp_path, manager)
    rec = AssessmentRecommendation(action="HOLD", confidence=80, rationale="All good.")
    result = manager.apply_assessment(tid, rec)
    assert result["status"] == "ok"
    assert "No parameter changes" in result["changes"][0]


def test_apply_hold_window_update(manager, tmp_path):
    from drift.models import AssessmentRecommendation

    tid = _seed_filled_trade(tmp_path, manager)
    rec = AssessmentRecommendation(
        action="ADJUST", confidence=70, rationale="Extend hold.",
        new_max_hold_minutes=90,
    )
    result = manager.apply_assessment(tid, rec)
    assert result["status"] == "ok"
    assert any("Hold" in c for c in result["changes"])
    pos = manager.get_position(tid)
    assert pos.max_hold_minutes == 90


def test_apply_to_closed_position_errors(manager, tmp_path):
    from drift.models import AssessmentRecommendation

    store = TradeStore(tmp_path / "test.db")
    tid = store.create(
        signal_key="MNQ:LONG:pullback:assess_closed",
        symbol="MNQ", bias="LONG", setup_type="pb",
        entry_min=19_000.0, entry_max=19_010.0,
        stop_loss=18_975.0, take_profit_1=19_040.0,
        take_profit_2=19_070.0, thesis="test",
    )
    store.close_trade(tid, "CLOSED_MANUAL")

    rec = AssessmentRecommendation(action="HOLD", confidence=80, rationale="x")
    result = manager.apply_assessment(tid, rec)
    assert result["status"] == "error"


def test_log_and_dismiss_assessment(manager, tmp_path):
    from drift.models import AssessmentRecommendation

    tid = _seed_filled_trade(tmp_path, manager)
    rec = AssessmentRecommendation(action="HOLD", confidence=80, rationale="Good.")
    aid = manager.log_assessment(tid, rec)
    assert aid >= 1
    manager.dismiss_assessment(aid)
