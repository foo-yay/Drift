"""Tests for the unified TradeStore."""
import pytest

from drift.storage.trade_store import TradeStore


@pytest.fixture()
def store(tmp_path):
    s = TradeStore(tmp_path / "test.db")
    yield s
    s.close()


_BASE = dict(
    signal_key="MNQ:LONG:pullback:1",
    symbol="MNQ",
    bias="LONG",
    setup_type="pullback_continuation",
    entry_min=19_000.0,
    entry_max=19_010.0,
    stop_loss=18_975.0,
    take_profit_1=19_040.0,
    take_profit_2=19_070.0,
    thesis="test thesis",
)


def test_create_returns_id(store):
    tid = store.create(**_BASE)
    assert tid >= 1


def test_duplicate_signal_key_ignored(store):
    tid1 = store.create(**_BASE)
    store.create(**_BASE)
    # Only one row should exist
    assert len(store.get_all()) == 1


def test_defaults(store):
    tid = store.create(**_BASE)
    row = store.get_by_id(tid)
    assert row.state == "PENDING"
    assert row.source == "live"
    assert row.exit_mode == "TP1"
    assert row.confidence == 0


def test_create_with_state(store):
    tid = store.create(**_BASE, state="WORKING", entry_limit=19_005.0, source="dev")
    row = store.get_by_id(tid)
    assert row.state == "WORKING"
    assert row.source == "dev"
    assert row.active_tp == 19_040.0  # set from take_profit_1


def test_set_state(store):
    tid = store.create(**_BASE)
    store.set_state(tid, "REJECTED", reject_reason="bad setup")
    row = store.get_by_id(tid)
    assert row.state == "REJECTED"
    assert row.reject_reason == "bad setup"


def test_set_broker_ids(store):
    tid = store.create(**_BASE, state="APPROVED")
    store.set_broker_ids(tid, entry_limit=19_005.0, parent_order_id=100,
                         tp_order_id=101, sl_order_id=102)
    row = store.get_by_id(tid)
    assert row.parent_order_id == 100
    assert row.tp_order_id == 101
    assert row.sl_order_id == 102
    assert row.entry_limit == 19_005.0


def test_mark_filled(store):
    tid = store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:2"},
                        state="WORKING", entry_limit=19_005.0)
    store.mark_filled(tid, fill_price=19_006.0)
    row = store.get_by_id(tid)
    assert row.state == "FILLED"
    assert row.entry_fill == 19_006.0
    assert row.fill_time is not None


def test_mark_filled_only_working(store):
    tid = store.create(**_BASE)  # PENDING
    store.mark_filled(tid, fill_price=19_006.0)
    row = store.get_by_id(tid)
    assert row.state == "PENDING"  # unchanged


def test_set_exit_mode(store):
    tid = store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:3"},
                        state="FILLED", entry_limit=19_005.0)
    store.set_exit_mode(tid, "TP2", active_tp=19_070.0)
    row = store.get_by_id(tid)
    assert row.exit_mode == "TP2"
    assert row.active_tp == 19_070.0


def test_close_trade(store):
    tid = store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:4"},
                        state="FILLED", entry_limit=19_005.0)
    store.close_trade(tid, "CLOSED_TP1", exit_price=19_040.0, exit_reason="TP1 hit")
    row = store.get_by_id(tid)
    assert row.state == "CLOSED_TP1"
    assert row.exit_price == 19_040.0
    assert row.exit_reason == "TP1 hit"


def test_expire_stale(store):
    import sqlite3
    # Manually backdate the created_at
    tid = store.create(**_BASE)
    store._conn.execute(
        "UPDATE trades SET created_at = datetime('now', '-2 hours') WHERE id=?",
        (tid,),
    )
    count = store.expire_stale(expiry_minutes=60)
    assert count == 1
    row = store.get_by_id(tid)
    assert row.state == "EXPIRED"


def test_get_pending(store):
    store.create(**_BASE)
    assert len(store.get_pending()) == 1


def test_get_active(store):
    store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:5"},
                  state="WORKING", entry_limit=19_005.0)
    store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:6"},
                  state="FILLED", entry_limit=19_005.0)
    assert len(store.get_active()) == 2


def test_get_open(store):
    store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:7"})  # PENDING
    store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:8"},
                  state="FILLED", entry_limit=19_005.0)
    assert len(store.get_open()) == 2


def test_has_active_trade(store):
    assert store.has_active_trade() is False
    store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:9"},
                  state="WORKING", entry_limit=19_005.0)
    assert store.has_active_trade() is True


def test_get_history(store):
    tid = store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:10"})
    store.set_state(tid, "REJECTED")
    assert len(store.get_history()) == 1
    assert len(store.get_pending()) == 0


def test_get_all(store):
    store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:11"})
    store.create(**{**_BASE, "signal_key": "MNQ:LONG:x:12"},
                  state="FILLED", entry_limit=19_005.0)
    assert len(store.get_all()) == 2
