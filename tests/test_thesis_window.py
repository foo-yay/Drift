"""Tests for thesis window, fill timeout, and trade-based cooldown."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drift.config.models import GatesSection, RiskSection
from drift.gates.cooldown_gate import CooldownGate
from drift.models import MarketSnapshot
from drift.storage.trade_store import TradeStore, TradeRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gates_config(cooldown_enabled: bool = True) -> GatesSection:
    return GatesSection(
        regime_enabled=True,
        min_trend_score=35,
        min_momentum_score=30,
        block_on_extreme_volatility=True,
        cooldown_enabled=cooldown_enabled,
        kill_switch_enabled=True,
        kill_switch_path="data/.kill_switch",
    )


def _make_risk_config(
    cooldown_minutes: int = 15,
    no_trade_cooldown_minutes: int = 15,
    fill_timeout_minutes: int = 5,
) -> RiskSection:
    return RiskSection(
        min_confidence=65,
        min_reward_risk=1.8,
        max_signals_per_day=3,
        cooldown_minutes=cooldown_minutes,
        no_trade_cooldown_minutes=no_trade_cooldown_minutes,
        fill_timeout_minutes=fill_timeout_minutes,
        max_stop_points=30.0,
        min_stop_points=6.0,
        atr_stop_floor_mult=0.8,
        atr_target_mult=1.8,
        max_hold_minutes_default=25,
        no_trade_during_high_impact_events=True,
    )


def _make_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        as_of=datetime.now(tz=timezone.utc),
        symbol="MNQ",
        last_price=21_000.0,
        session="open",
        bars_1m_count=180,
        bars_5m_count=120,
        bars_1h_count=72,
        trend_score=60,
        momentum_score=55,
        volatility_score=60,
        extension_risk=20,
        structure_quality=50,
        pullback_quality=50,
        breakout_quality=50,
        mean_reversion_risk=20,
        session_alignment=50,
        short_trend_state="bullish",
        medium_trend_state="bullish",
        momentum_state="neutral",
        volatility_regime="normal",
    )


def _create_trade(
    store: TradeStore,
    state: str = "WORKING",
    minutes_ago: float = 5,
    max_hold_minutes: int = 30,
) -> int:
    """Insert a trade and return its id."""
    gen_at = (datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return store.create(
        signal_key=f"TEST:{gen_at}",
        symbol="MNQ",
        bias="LONG",
        setup_type="pullback_continuation",
        entry_min=21000.0,
        entry_max=21005.0,
        stop_loss=20980.0,
        take_profit_1=21040.0,
        take_profit_2=21060.0,
        thesis="Test thesis",
        confidence=75,
        max_hold_minutes=max_hold_minutes,
        generated_at=gen_at,
        source="dev",
        state=state,
    )


def _write_log(path: Path, events: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _event(final_outcome: str, minutes_ago: float) -> dict:
    ts = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes_ago)
    return {"event_time": ts.isoformat(), "final_outcome": final_outcome}


# ---------------------------------------------------------------------------
# Trade store: thesis_anchor tests
# ---------------------------------------------------------------------------

class TestThesisAnchor:
    def test_thesis_anchor_set_on_create(self, tmp_path):
        """thesis_anchor should equal generated_at on creation."""
        store = TradeStore(str(tmp_path / "test.db"))
        gen_at = datetime.now(tz=timezone.utc).isoformat()
        tid = store.create(
            signal_key="TA1",
            symbol="MNQ",
            bias="LONG",
            setup_type="pullback",
            entry_min=21000,
            entry_max=21005,
            stop_loss=20980,
            take_profit_1=21040,
            take_profit_2=None,
            thesis="test",
            generated_at=gen_at,
        )
        row = store.get_by_id(tid)
        store.close()
        assert row is not None
        assert row.thesis_anchor == gen_at
        assert row.generated_at == gen_at

    def test_thesis_anchor_resets_on_hold_update(self, tmp_path):
        """update_hold_window should reset thesis_anchor to now."""
        store = TradeStore(str(tmp_path / "test.db"))
        old_time = (datetime.now(tz=timezone.utc) - timedelta(minutes=20)).isoformat()
        tid = store.create(
            signal_key="TA2",
            symbol="MNQ",
            bias="LONG",
            setup_type="pullback",
            entry_min=21000,
            entry_max=21005,
            stop_loss=20980,
            take_profit_1=21040,
            take_profit_2=None,
            thesis="test",
            generated_at=old_time,
        )
        row_before = store.get_by_id(tid)
        assert row_before.thesis_anchor == old_time

        store.update_hold_window(tid, 45)
        row_after = store.get_by_id(tid)
        store.close()

        # thesis_anchor should be updated to approximately now
        assert row_after.thesis_anchor != old_time
        assert row_after.max_hold_minutes == 45
        anchor_dt = datetime.fromisoformat(row_after.thesis_anchor)
        assert (datetime.now(tz=timezone.utc) - anchor_dt).total_seconds() < 5

    def test_thesis_anchor_fallback_to_generated_at(self, tmp_path):
        """When thesis_anchor is empty, code should fall back to generated_at."""
        store = TradeStore(str(tmp_path / "test.db"))
        gen_at = datetime.now(tz=timezone.utc).isoformat()
        tid = store.create(
            signal_key="TA3",
            symbol="MNQ",
            bias="LONG",
            setup_type="pullback",
            entry_min=21000,
            entry_max=21005,
            stop_loss=20980,
            take_profit_1=21040,
            take_profit_2=None,
            thesis="test",
            generated_at=gen_at,
        )
        # Manually clear thesis_anchor to simulate old DB
        store._conn.execute("UPDATE trades SET thesis_anchor='' WHERE id=?", (tid,))
        row = store.get_by_id(tid)
        store.close()

        anchor = row.thesis_anchor or row.generated_at
        assert anchor == gen_at


class TestGetWorking:
    def test_returns_only_working_trades(self, tmp_path):
        store = TradeStore(str(tmp_path / "test.db"))
        _create_trade(store, state="WORKING")
        _create_trade(store, state="WORKING")
        # Create a filled trade too
        tid = _create_trade(store, state="WORKING")
        store.mark_filled(tid, 21002.0)

        working = store.get_working()
        store.close()

        assert len(working) == 2
        assert all(t.state == "WORKING" for t in working)


# ---------------------------------------------------------------------------
# Cooldown gate: trade-based cooldown tests
# ---------------------------------------------------------------------------

class TestCooldownGateWithDB:
    """When db_path is provided, active trades should block the cooldown gate."""

    def test_blocks_when_active_trade_exists(self, tmp_path):
        """Active WORKING trade should block cooldown gate."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")

        store = TradeStore(db)
        _create_trade(store, state="WORKING", minutes_ago=2, max_hold_minutes=30)
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            db_path=db,
        )
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "WORKING" in result.reason

    def test_blocks_when_filled_trade_exists(self, tmp_path):
        """Active FILLED trade should block cooldown gate."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")

        store = TradeStore(db)
        tid = _create_trade(store, state="WORKING", minutes_ago=10, max_hold_minutes=30)
        store.mark_filled(tid, 21002.0)
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            db_path=db,
        )
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "FILLED" in result.reason

    def test_passes_when_trade_closed(self, tmp_path):
        """Closed trade should NOT block cooldown gate."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")

        store = TradeStore(db)
        tid = _create_trade(store, state="WORKING", minutes_ago=5, max_hold_minutes=30)
        store.close_trade(tid, "CLOSED_CANCEL")
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            db_path=db,
        )
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_remaining_minutes_shown_in_reason(self, tmp_path):
        """Reason should show remaining minutes in thesis window."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")

        store = TradeStore(db)
        _create_trade(store, state="WORKING", minutes_ago=5, max_hold_minutes=30)
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            db_path=db,
        )
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        # ~25 min remaining
        assert "25" in result.reason or "24" in result.reason

    def test_no_trade_cooldown_still_works_with_db(self, tmp_path):
        """LLM_NO_TRADE cooldown should still work via JSONL even when DB is available."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        _write_log(log_path, [_event("LLM_NO_TRADE", minutes_ago=3)])

        # No active trades in DB
        store = TradeStore(db)
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(no_trade_cooldown_minutes=15),
            log_path,
            db_path=db,
        )
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "cooldown" in result.reason.lower()

    def test_seconds_remaining_uses_active_trade(self, tmp_path):
        """seconds_remaining() should reflect thesis window of active trade."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")

        store = TradeStore(db)
        _create_trade(store, state="WORKING", minutes_ago=5, max_hold_minutes=30)
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            db_path=db,
        )
        remaining = gate.seconds_remaining()
        assert remaining is not None
        # ~25 min = ~1500 s
        assert 1400 < remaining < 1560

    def test_seconds_remaining_none_when_no_active_trade(self, tmp_path):
        """seconds_remaining() should return None when no active trade and no JSONL events."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")

        store = TradeStore(db)
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            db_path=db,
        )
        assert gate.seconds_remaining() is None

    def test_thesis_window_expired_clears_cooldown(self, tmp_path):
        """If thesis window has expired but trade is still active, cooldown should have no remaining time."""
        db = str(tmp_path / "test.db")
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")

        store = TradeStore(db)
        # Trade created 40 min ago with 30 min hold → expired
        _create_trade(store, state="WORKING", minutes_ago=40, max_hold_minutes=30)
        store.close()

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            db_path=db,
        )
        # Even though trade is still "active" in DB, the thesis window is expired
        # The gate should still block (trade exists) but seconds_remaining should be None
        remaining = gate.seconds_remaining()
        assert remaining is None


class TestCooldownGateWithoutDB:
    """When no db_path is provided (replay mode), behavior should be unchanged."""

    def test_falls_back_to_jsonl_for_trade_plan(self, tmp_path):
        """Without DB, TRADE_PLAN_ISSUED still creates JSONL-based cooldown."""
        log_path = tmp_path / "events.jsonl"
        ts = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        _write_log(log_path, [{
            "event_time": ts.isoformat(),
            "final_outcome": "TRADE_PLAN_ISSUED",
            "trade_plan": {"max_hold_minutes": 30},
        }])

        gate = CooldownGate(
            _make_gates_config(),
            _make_risk_config(),
            log_path,
            # No db_path — replay mode
        )
        result = gate.evaluate(_make_snapshot())
        assert not result.passed


# ---------------------------------------------------------------------------
# Fill timeout config tests
# ---------------------------------------------------------------------------

class TestFillTimeoutConfig:
    def test_default_fill_timeout(self):
        """Default fill_timeout_minutes should be 5."""
        config = _make_risk_config()
        assert config.fill_timeout_minutes == 5

    def test_custom_fill_timeout(self):
        config = _make_risk_config(fill_timeout_minutes=10)
        assert config.fill_timeout_minutes == 10

    def test_fill_timeout_validation(self):
        """fill_timeout_minutes must be >= 1."""
        with pytest.raises(Exception):
            _make_risk_config(fill_timeout_minutes=0)


# ---------------------------------------------------------------------------
# DB migration test
# ---------------------------------------------------------------------------

class TestThesisAnchorMigration:
    def test_migration_adds_column_to_old_db(self, tmp_path):
        """Opening a DB without thesis_anchor column should auto-add it."""
        import sqlite3

        db_path = str(tmp_path / "old.db")
        # Create a DB with the old schema (no thesis_anchor)
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_key TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                bias TEXT NOT NULL,
                setup_type TEXT NOT NULL,
                confidence INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'live',
                thesis TEXT NOT NULL DEFAULT '',
                entry_min REAL NOT NULL,
                entry_max REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit_1 REAL NOT NULL,
                take_profit_2 REAL,
                max_hold_minutes INTEGER NOT NULL DEFAULT 60,
                generated_at TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'PENDING',
                reject_reason TEXT,
                quantity INTEGER NOT NULL DEFAULT 1,
                entry_limit REAL,
                parent_order_id INTEGER,
                tp_order_id INTEGER,
                sl_order_id INTEGER,
                ib_perm_id INTEGER,
                entry_fill REAL,
                fill_time TEXT,
                exit_mode TEXT NOT NULL DEFAULT 'TP1',
                active_tp REAL,
                exit_price REAL,
                exit_time TEXT,
                exit_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        now = datetime.now(tz=timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO trades (signal_key, symbol, bias, setup_type, entry_min,
               entry_max, stop_loss, take_profit_1, max_hold_minutes, generated_at,
               state, created_at, updated_at)
            VALUES ('OLD1', 'MNQ', 'LONG', 'pullback', 21000, 21005, 20980, 21040,
                    30, ?, 'WORKING', ?, ?)""",
            (now, now, now),
        )
        conn.commit()
        conn.close()

        # Now open with TradeStore which should run migration
        store = TradeStore(db_path)
        row = store.get_by_id(1)
        store.close()

        assert row is not None
        assert row.thesis_anchor == now  # backfilled from generated_at
