"""Tests for PendingOrderStore."""
from __future__ import annotations

import time

import pytest

from drift.storage.pending_order_store import PendingOrderStore


@pytest.fixture()
def store(tmp_path):
    s = PendingOrderStore(tmp_path / "test.db")
    yield s
    s.close()


def _create(store: PendingOrderStore, key: str = "sig_1") -> int:
    return store.create(
        signal_key=key,
        symbol="MNQ",
        bias="LONG",
        setup_type="breakout",
        confidence=75,
        entry_min=21000.0,
        entry_max=21010.0,
        stop_loss=20980.0,
        take_profit_1=21050.0,
        take_profit_2=21080.0,
        thesis="test thesis",
    )


def test_create_and_get(store):
    row_id = _create(store)
    assert row_id > 0
    row = store.get_by_id(row_id)
    assert row is not None
    assert row.state == "PENDING"
    assert row.bias == "LONG"
    assert row.entry_max == 21010.0


def test_duplicate_signal_key_ignored(store):
    _create(store, "dup")
    _create(store, "dup")
    # Only one row should exist despite two inserts
    assert len(store.get_all()) == 1


def test_set_state(store):
    row_id = _create(store)
    store.set_state(row_id, "SUBMITTED", ib_order_id=42, ib_perm_id=99)
    row = store.get_by_id(row_id)
    assert row.state == "SUBMITTED"
    assert row.ib_order_id == 42
    assert row.ib_perm_id == 99


def test_get_pending(store):
    _create(store, "a")
    _create(store, "b")
    id3 = _create(store, "c")
    store.set_state(id3, "REJECTED", reject_reason="manual")
    assert len(store.get_pending()) == 2


def test_get_all(store):
    _create(store, "x")
    _create(store, "y")
    assert len(store.get_all()) == 2


def test_expire_stale(store):
    _create(store, "old")
    # Manually backdate created_at by 2 hours
    store._conn.execute(
        "UPDATE pending_orders SET created_at = datetime('now', '-2 hours')"
    )
    expired = store.expire_stale(expiry_minutes=15)
    assert expired == 1
    assert len(store.get_pending()) == 0
