"""Active position store — tracks live positions from entry fill to exit.

Lifecycle:
    WORKING   — entry order placed, not yet filled
    FILLED    — entry filled; SL and TP orders are working on IB
    CLOSED_TP1    — TP1 limit filled
    CLOSED_TP2    — TP2 limit filled (operator extended target)
    CLOSED_SL     — stop loss triggered
    CLOSED_MANUAL — operator clicked Manual Close
    CLOSED_CANCEL — operator cancelled before fill

Exit mode (mutable while FILLED):
    TP1      — default; TP1 limit order active
    TP2      — operator toggled; TP1 cancelled, TP2 placed
    MANUAL   — operator holding; TP cancelled, only SL remains
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS active_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_order_id INTEGER NOT NULL,       -- FK to pending_orders.id
    signal_key      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    bias            TEXT NOT NULL,           -- LONG | SHORT
    setup_type      TEXT NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 1,

    -- Entry
    entry_limit     REAL NOT NULL,           -- original limit price
    entry_fill      REAL,                    -- actual fill price (set on FILLED)
    fill_time       TEXT,                    -- ISO timestamp of fill

    -- Targets & stops
    stop_loss       REAL NOT NULL,
    take_profit_1   REAL NOT NULL,
    take_profit_2   REAL,
    active_tp       REAL,                    -- currently active TP price on IB

    -- IB order IDs
    parent_order_id  INTEGER,
    tp_order_id      INTEGER,
    sl_order_id      INTEGER,

    -- Exit mode & state
    exit_mode       TEXT NOT NULL DEFAULT 'TP1',  -- TP1 | TP2 | MANUAL
    state           TEXT NOT NULL DEFAULT 'WORKING',
    exit_price      REAL,
    exit_time       TEXT,
    exit_reason     TEXT,                    -- human-readable exit note

    -- Time control
    max_hold_minutes INTEGER NOT NULL DEFAULT 60,
    thesis          TEXT NOT NULL DEFAULT '',

    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ap_state ON active_positions(state);
"""


@dataclass
class ActivePositionRow:
    id: int
    pending_order_id: int
    signal_key: str
    symbol: str
    bias: str
    setup_type: str
    quantity: int
    entry_limit: float
    entry_fill: float | None
    fill_time: str | None
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    active_tp: float | None
    parent_order_id: int | None
    tp_order_id: int | None
    sl_order_id: int | None
    exit_mode: str
    state: str
    exit_price: float | None
    exit_time: str | None
    exit_reason: str | None
    max_hold_minutes: int
    thesis: str
    created_at: str
    updated_at: str


class PositionStore:
    """Thread-compatible SQLite store for active IB positions."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create(
        self,
        pending_order_id: int,
        signal_key: str,
        symbol: str,
        bias: str,
        setup_type: str,
        entry_limit: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float | None,
        max_hold_minutes: int,
        thesis: str,
        parent_order_id: int | None = None,
        tp_order_id: int | None = None,
        sl_order_id: int | None = None,
        quantity: int = 1,
    ) -> int:
        """Insert a new WORKING position.  Returns the new row id."""
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO active_positions
                (pending_order_id, signal_key, symbol, bias, setup_type,
                 quantity, entry_limit, stop_loss, take_profit_1, take_profit_2,
                 active_tp, parent_order_id, tp_order_id, sl_order_id,
                 exit_mode, state, max_hold_minutes, thesis,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'TP1','WORKING',?,?,?,?)
            """,
            (pending_order_id, signal_key, symbol, bias, setup_type,
             quantity, entry_limit, stop_loss, take_profit_1, take_profit_2,
             take_profit_1,  # active_tp starts at TP1
             parent_order_id, tp_order_id, sl_order_id,
             max_hold_minutes, thesis, now, now),
        )
        return cur.lastrowid or 0

    def mark_filled(
        self, row_id: int, fill_price: float, fill_time: str | None = None,
    ) -> None:
        """Transition from WORKING to FILLED."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE active_positions
            SET state='FILLED', entry_fill=?, fill_time=?, updated_at=?
            WHERE id=? AND state='WORKING'
            """,
            (fill_price, fill_time or now, now, row_id),
        )

    def set_exit_mode(
        self,
        row_id: int,
        exit_mode: str,
        active_tp: float | None,
        tp_order_id: int | None = None,
    ) -> None:
        """Change exit mode (TP1/TP2/MANUAL) and update the active TP price."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE active_positions
            SET exit_mode=?, active_tp=?,
                tp_order_id=COALESCE(?, tp_order_id),
                updated_at=?
            WHERE id=?
            """,
            (exit_mode, active_tp, tp_order_id, now, row_id),
        )

    def set_ib_order_ids(
        self,
        row_id: int,
        parent_order_id: int | None = None,
        tp_order_id: int | None = None,
        sl_order_id: int | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE active_positions
            SET parent_order_id=COALESCE(?, parent_order_id),
                tp_order_id=COALESCE(?, tp_order_id),
                sl_order_id=COALESCE(?, sl_order_id),
                updated_at=?
            WHERE id=?
            """,
            (parent_order_id, tp_order_id, sl_order_id, now, row_id),
        )

    def close_position(
        self,
        row_id: int,
        state: str,
        exit_price: float | None = None,
        exit_reason: str = "",
    ) -> None:
        """Mark a position as closed (terminal state)."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE active_positions
            SET state=?, exit_price=?, exit_time=?, exit_reason=?, updated_at=?
            WHERE id=?
            """,
            (state, exit_price, now, exit_reason, now, row_id),
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    _OPEN_STATES = ("WORKING", "FILLED")

    def get_open(self) -> list[ActivePositionRow]:
        """Return all non-terminal positions."""
        rows = self._conn.execute(
            "SELECT * FROM active_positions WHERE state IN ('WORKING','FILLED') ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_filled(self) -> list[ActivePositionRow]:
        """Return only FILLED positions."""
        rows = self._conn.execute(
            "SELECT * FROM active_positions WHERE state='FILLED' ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_by_id(self, row_id: int) -> ActivePositionRow | None:
        r = self._conn.execute(
            "SELECT * FROM active_positions WHERE id=?", (row_id,)
        ).fetchone()
        return _row(r) if r else None

    def get_all(self, limit: int = 50) -> list[ActivePositionRow]:
        rows = self._conn.execute(
            "SELECT * FROM active_positions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row(r) for r in rows]

    def has_open_position(self) -> bool:
        """Check if any position is WORKING or FILLED (duplicate guard)."""
        r = self._conn.execute(
            "SELECT 1 FROM active_positions WHERE state IN ('WORKING','FILLED') LIMIT 1"
        ).fetchone()
        return r is not None

    def close(self) -> None:
        self._conn.close()


def _row(r: sqlite3.Row) -> ActivePositionRow:
    return ActivePositionRow(
        id=r["id"],
        pending_order_id=r["pending_order_id"],
        signal_key=r["signal_key"],
        symbol=r["symbol"],
        bias=r["bias"],
        setup_type=r["setup_type"],
        quantity=r["quantity"],
        entry_limit=r["entry_limit"],
        entry_fill=r["entry_fill"],
        fill_time=r["fill_time"],
        stop_loss=r["stop_loss"],
        take_profit_1=r["take_profit_1"],
        take_profit_2=r["take_profit_2"],
        active_tp=r["active_tp"],
        parent_order_id=r["parent_order_id"],
        tp_order_id=r["tp_order_id"],
        sl_order_id=r["sl_order_id"],
        exit_mode=r["exit_mode"],
        state=r["state"],
        exit_price=r["exit_price"],
        exit_time=r["exit_time"],
        exit_reason=r["exit_reason"],
        max_hold_minutes=r["max_hold_minutes"],
        thesis=r["thesis"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )
