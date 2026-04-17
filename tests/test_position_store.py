"""Tests for PositionStore — CRUD, state transitions, duplicate guard."""
import pytest

from drift.storage.position_store import PositionStore


@pytest.fixture()
def store(tmp_path):
    return PositionStore(tmp_path / "test.db")


def _create(store, **kwargs):
    defaults = dict(
        pending_order_id=1,
        signal_key="MNQ:LONG:pullback_continuation:1234",
        symbol="MNQ",
        bias="LONG",
        setup_type="pullback_continuation",
        entry_limit=19_000.0,
        stop_loss=18_980.0,
        take_profit_1=19_030.0,
        take_profit_2=19_060.0,
        max_hold_minutes=30,
        thesis="Test thesis",
    )
    defaults.update(kwargs)
    return store.create(**defaults)


# ------------------------------------------------------------------
# Basic CRUD
# ------------------------------------------------------------------

def test_create_returns_id(store):
    pos_id = _create(store)
    assert isinstance(pos_id, int)
    assert pos_id >= 1


def test_initial_state_is_working(store):
    pos_id = _create(store)
    rows = store.get_open()
    assert len(rows) == 1
    assert rows[0].id == pos_id
    assert rows[0].state == "WORKING"
    assert rows[0].exit_mode == "TP1"


def test_get_all_returns_row(store):
    _create(store)
    rows = store.get_all()
    assert len(rows) == 1


# ------------------------------------------------------------------
# mark_filled
# ------------------------------------------------------------------

def test_mark_filled_transitions_state(store):
    pos_id = _create(store)
    store.mark_filled(pos_id, fill_price=19_001.5, fill_time="2026-04-17T10:00:00+00:00")
    filled = store.get_filled()
    assert len(filled) == 1
    row = filled[0]
    assert row.state == "FILLED"
    assert row.entry_fill == 19_001.5
    assert row.fill_time == "2026-04-17T10:00:00+00:00"


def test_mark_filled_removes_from_working(store):
    pos_id = _create(store)
    store.mark_filled(pos_id, fill_price=19_000.0, fill_time="2026-04-17T10:00:00+00:00")
    # get_open returns both WORKING and FILLED; verify no WORKING rows remain
    open_rows = store.get_open()
    assert all(r.state != "WORKING" for r in open_rows)


# ------------------------------------------------------------------
# set_exit_mode
# ------------------------------------------------------------------

def test_set_exit_mode(store):
    pos_id = _create(store)
    store.set_exit_mode(pos_id, "TP2", active_tp=19_060.0)
    rows = store.get_open()
    assert rows[0].exit_mode == "TP2"
    assert rows[0].active_tp == 19_060.0


def test_set_exit_mode_manual(store):
    pos_id = _create(store)
    store.set_exit_mode(pos_id, "MANUAL", active_tp=None)
    rows = store.get_open()
    assert rows[0].exit_mode == "MANUAL"
    assert rows[0].active_tp is None


def test_set_exit_mode_hold_expiry(store):
    pos_id = _create(store)
    store.set_exit_mode(pos_id, "HOLD_EXPIRY", active_tp=None)
    rows = store.get_open()
    assert rows[0].exit_mode == "HOLD_EXPIRY"
    assert rows[0].active_tp is None


# ------------------------------------------------------------------
# close_position
# ------------------------------------------------------------------

def test_close_position(store):
    pos_id = _create(store)
    store.mark_filled(pos_id, 19_001.0, "2026-04-17T10:00:00+00:00")
    store.close_position(pos_id, state="CLOSED_TP1", exit_price=19_030.0, exit_reason="TP1 filled")
    assert store.get_filled() == []
    all_rows = store.get_all()
    closed = [r for r in all_rows if r.state == "CLOSED_TP1"]
    assert len(closed) == 1
    assert closed[0].exit_price == 19_030.0


# ------------------------------------------------------------------
# Duplicate position guard
# ------------------------------------------------------------------

def test_has_open_position_false_when_empty(store):
    assert store.has_open_position() is False


def test_has_open_position_true_when_working(store):
    _create(store)
    assert store.has_open_position() is True


def test_has_open_position_true_when_filled(store):
    pos_id = _create(store)
    store.mark_filled(pos_id, 19_000.0, "2026-04-17T10:00:00+00:00")
    assert store.has_open_position() is True


def test_has_open_position_false_after_close(store):
    pos_id = _create(store)
    store.mark_filled(pos_id, 19_000.0, "2026-04-17T10:00:00+00:00")
    store.close_position(pos_id, state="CLOSED_MANUAL", exit_price=19_010.0, exit_reason="manual")
    assert store.has_open_position() is False


# ------------------------------------------------------------------
# set_ib_order_ids
# ------------------------------------------------------------------

def test_set_ib_order_ids(store):
    pos_id = _create(store)
    store.set_ib_order_ids(pos_id, parent_order_id=101, tp_order_id=102, sl_order_id=103)
    rows = store.get_open()
    assert rows[0].parent_order_id == 101
    assert rows[0].tp_order_id == 102
    assert rows[0].sl_order_id == 103
