"""Tests for drift.storage.signal_store and drift.storage.migrator."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from drift.models import SignalEvent
from drift.storage.migrator import migrate_jsonl
from drift.storage.signal_store import SignalStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 4, 25, 14, 30, 0, tzinfo=timezone.utc)


def _make_event(
    symbol: str = "MNQ",
    source: str = "live",
    outcome: str = "TRADE_PLAN_ISSUED",
    as_of: str | None = None,
    **kwargs,
) -> SignalEvent:
    snap = {"as_of": as_of or _TS.isoformat()} if as_of is not None else {"as_of": _TS.isoformat()}
    return SignalEvent(
        event_time=_TS,
        symbol=symbol,
        source=source,
        snapshot=snap,
        final_outcome=outcome,
        final_reason="test",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# SignalStore — schema creation
# ---------------------------------------------------------------------------

class TestSignalStoreSchema:
    def test_creates_tables_in_memory(self) -> None:
        store = SignalStore(":memory:")
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in rows}
        assert "signals" in table_names
        assert "replay_runs" in table_names
        store.close()

    def test_creates_db_file(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        store = SignalStore(db)
        assert db.exists()
        store.close()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        db = tmp_path / "nested" / "dir" / "test.db"
        store = SignalStore(db)
        assert db.exists()
        store.close()


# ---------------------------------------------------------------------------
# SignalStore — insert & deduplication
# ---------------------------------------------------------------------------

class TestInsertAndDedup:
    def _store(self) -> SignalStore:
        return SignalStore(":memory:")

    def test_insert_returns_true(self) -> None:
        store = self._store()
        event = _make_event()
        assert store.insert_event(event) is True
        store.close()

    def test_duplicate_insert_returns_false(self) -> None:
        store = self._store()
        event = _make_event()
        store.insert_event(event)
        # Same symbol + as_of + source → same signal_key → duplicate
        assert store.insert_event(event) is False
        store.close()

    def test_different_as_of_inserts_both(self) -> None:
        store = self._store()
        e1 = _make_event(as_of="2026-04-25T14:30:00+00:00")
        e2 = _make_event(as_of="2026-04-25T14:45:00+00:00")
        assert store.insert_event(e1) is True
        assert store.insert_event(e2) is True
        assert len(store.query()) == 2
        store.close()

    def test_different_source_inserts_both(self) -> None:
        store = self._store()
        e1 = _make_event(source="live")
        e2 = _make_event(source="replay")
        assert store.insert_event(e1) is True
        assert store.insert_event(e2) is True
        store.close()

    def test_signal_key_populated_after_insert(self) -> None:
        store = self._store()
        event = _make_event()
        assert event.signal_key is None  # starts unpopulated
        store.insert_event(event)
        row = store.query()[0]
        assert row.signal_key is not None
        assert len(row.signal_key) == 16
        store.close()


# ---------------------------------------------------------------------------
# SignalStore — query
# ---------------------------------------------------------------------------

class TestQuery:
    def _store_with_events(self) -> SignalStore:
        store = SignalStore(":memory:")
        store.insert_event(_make_event(symbol="MNQ", source="live", outcome="TRADE_PLAN_ISSUED", as_of="2026-04-25T14:30:00+00:00"))
        store.insert_event(_make_event(symbol="MNQ", source="replay", outcome="LLM_NO_TRADE", as_of="2026-04-25T14:45:00+00:00"))
        store.insert_event(_make_event(symbol="ES", source="live", outcome="BLOCKED", as_of="2026-04-25T15:00:00+00:00"))
        return store

    def test_query_all(self) -> None:
        store = self._store_with_events()
        assert len(store.query()) == 3
        store.close()

    def test_query_by_symbol(self) -> None:
        store = self._store_with_events()
        rows = store.query(symbol="MNQ")
        assert len(rows) == 2
        assert all(r.symbol == "MNQ" for r in rows)
        store.close()

    def test_query_by_source(self) -> None:
        store = self._store_with_events()
        rows = store.query(sources=["live"])
        assert len(rows) == 2
        assert all(r.source == "live" for r in rows)
        store.close()

    def test_query_trade_plans_only(self) -> None:
        store = self._store_with_events()
        rows = store.query(trade_plans_only=True)
        assert len(rows) == 1
        assert rows[0].final_outcome == "TRADE_PLAN_ISSUED"
        store.close()

    def test_query_by_date_range(self) -> None:
        store = self._store_with_events()
        rows = store.query(date_start=date(2026, 4, 25), date_end=date(2026, 4, 25))
        assert len(rows) == 3
        store.close()

    def test_query_empty_date_range(self) -> None:
        store = self._store_with_events()
        rows = store.query(date_start=date(2026, 4, 26), date_end=date(2026, 4, 26))
        assert len(rows) == 0
        store.close()

    def test_get_by_key(self) -> None:
        store = SignalStore(":memory:")
        event = _make_event()
        store.insert_event(event)
        row = store.query()[0]
        fetched = store.get_by_key(row.signal_key)
        assert fetched is not None
        assert fetched.signal_key == row.signal_key
        store.close()

    def test_get_by_key_missing_returns_none(self) -> None:
        store = SignalStore(":memory:")
        assert store.get_by_key("0000000000000000") is None
        store.close()


# ---------------------------------------------------------------------------
# SignalStore — delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_by_key(self) -> None:
        store = SignalStore(":memory:")
        store.insert_event(_make_event())
        row = store.query()[0]
        store.delete_by_key(row.signal_key)
        assert store.query() == []
        store.close()

    def test_delete_by_date_range(self) -> None:
        store = SignalStore(":memory:")
        store.insert_event(_make_event(source="replay", as_of="2026-04-23T14:30:00+00:00"))
        store.insert_event(_make_event(source="replay", as_of="2026-04-24T14:30:00+00:00"))
        store.insert_event(_make_event(source="live", as_of="2026-04-23T14:30:00+00:00"))
        deleted = store.delete_by_date_range("MNQ", date(2026, 4, 23), date(2026, 4, 24), source="replay")
        assert deleted == 2
        remaining = store.query()
        # Live signal survives
        assert len(remaining) == 1
        assert remaining[0].source == "live"
        store.close()

    def test_count_by_date_range(self) -> None:
        store = SignalStore(":memory:")
        store.insert_event(_make_event(source="replay", as_of="2026-04-23T14:30:00+00:00"))
        store.insert_event(_make_event(source="replay", as_of="2026-04-24T14:45:00+00:00"))
        count = store.count_by_date_range("MNQ", date(2026, 4, 23), date(2026, 4, 24), source="replay")
        assert count == 2
        store.close()


# ---------------------------------------------------------------------------
# SignalStore — outcome update
# ---------------------------------------------------------------------------

class TestOutcomeUpdate:
    def test_upsert_outcome(self) -> None:
        store = SignalStore(":memory:")
        store.insert_event(_make_event())
        row = store.query()[0]
        assert row.replay_outcome is None
        store.upsert_outcome(row.signal_key, "TP1_HIT", 12.5)
        updated = store.get_by_key(row.signal_key)
        assert updated is not None
        assert updated.replay_outcome == "TP1_HIT"
        assert updated.pnl_points == pytest.approx(12.5)
        store.close()


# ---------------------------------------------------------------------------
# SignalStore — aggregate stats
# ---------------------------------------------------------------------------

class TestAggregateStats:
    def _populated_store(self) -> SignalStore:
        store = SignalStore(":memory:")
        e1 = _make_event(as_of="2026-04-25T14:30:00+00:00")
        e2 = _make_event(as_of="2026-04-25T14:45:00+00:00")
        store.insert_event(e1)
        store.insert_event(e2)
        rows = store.query()
        store.upsert_outcome(rows[0].signal_key, "TP1_HIT", 10.0)
        store.upsert_outcome(rows[1].signal_key, "STOP_HIT", -8.0)
        return store

    def test_win_rate_and_pnl(self) -> None:
        store = self._populated_store()
        stats = store.win_rate_and_pnl()
        assert stats["total"] == 2
        assert stats["resolved"] == 2
        assert stats["wins"] == 1
        assert stats["win_rate_pct"] == pytest.approx(50.0)
        assert stats["total_pnl"] == pytest.approx(2.0)
        store.close()

    def test_entry_missed_excluded_from_resolved(self) -> None:
        """ENTRY_MISSED trades must not count as resolved — they never filled."""
        store = SignalStore(":memory:")
        e1 = _make_event(as_of="2026-04-25T14:30:00+00:00")
        e2 = _make_event(as_of="2026-04-25T14:45:00+00:00")
        e3 = _make_event(as_of="2026-04-25T15:00:00+00:00")
        for e in (e1, e2, e3):
            store.insert_event(e)
        rows = store.query()
        store.upsert_outcome(rows[0].signal_key, "TP1_HIT", 31.6)   # real win
        store.upsert_outcome(rows[1].signal_key, "ENTRY_MISSED", 0.0)
        store.upsert_outcome(rows[2].signal_key, "ENTRY_MISSED", 0.0)
        stats = store.win_rate_and_pnl()
        # Only the TP1_HIT counts — ENTRY_MISSED trades are not resolved fills
        assert stats["resolved"] == 1
        assert stats["wins"] == 1
        assert stats["win_rate_pct"] == pytest.approx(100.0)
        assert stats["total_pnl"] == pytest.approx(31.6)
        store.close()

    def test_stats_no_signals(self) -> None:
        store = SignalStore(":memory:")
        stats = store.win_rate_and_pnl()
        assert stats["total"] == 0
        assert stats["win_rate_pct"] == 0.0
        store.close()


# ---------------------------------------------------------------------------
# Migrator
# ---------------------------------------------------------------------------

class TestMigrator:
    def _write_jsonl(self, path: Path, events: list[SignalEvent]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e.model_dump(mode="json"), sort_keys=True) + "\n")

    def test_migrate_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.touch()
        result = migrate_jsonl(p, tmp_path / "test.db")
        assert result.migrated == 0
        assert result.errors == 0

    def test_migrate_missing_file(self, tmp_path: Path) -> None:
        result = migrate_jsonl(tmp_path / "missing.jsonl", tmp_path / "test.db")
        assert result.migrated == 0
        assert result.errors == 0

    def test_migrate_inserts_events(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        self._write_jsonl(p, [
            _make_event(as_of="2026-04-25T14:30:00+00:00"),
            _make_event(as_of="2026-04-25T14:45:00+00:00"),
        ])
        db = tmp_path / "test.db"
        result = migrate_jsonl(p, db)
        assert result.migrated == 2
        assert result.skipped == 0
        assert result.errors == 0
        # Verify data in DB
        store = SignalStore(db)
        assert len(store.query()) == 2
        store.close()

    def test_migrate_idempotent(self, tmp_path: Path) -> None:
        """Running migration twice must not insert duplicates."""
        p = tmp_path / "events.jsonl"
        self._write_jsonl(p, [_make_event()])
        db = tmp_path / "test.db"
        r1 = migrate_jsonl(p, db)
        r2 = migrate_jsonl(p, db)
        assert r1.migrated == 1
        assert r2.migrated == 0
        assert r2.skipped == 1
        store = SignalStore(db)
        assert len(store.query()) == 1
        store.close()

    def test_migrate_skips_malformed_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with p.open("w") as f:
            f.write("{not valid json}\n")
            f.write(json.dumps(_make_event().model_dump(mode="json")) + "\n")
        db = tmp_path / "test.db"
        result = migrate_jsonl(p, db)
        assert result.migrated == 1
        assert result.errors == 1
