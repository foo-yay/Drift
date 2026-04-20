"""Unified trade store — single table tracking the full trade lifecycle.

Replaces the old ``PendingOrderStore`` + ``PositionStore`` split with one row
per trade that progresses through a single state machine:

    PENDING → REJECTED | EXPIRED
    PENDING → APPROVED → WORKING → FILLED → CLOSED_*
    WORKING → CLOSED_CANCEL

Each row carries all fields needed from signal approval through exit.  Fields
that only apply to later phases (e.g. ``entry_fill``, ``exit_price``) start
``NULL`` and are populated as the trade progresses.

The ``source`` column tags the origin:
    live    — real signal from the scheduler/LLM pipeline
    sandbox — sandbox-mode signal
    dev     — seeded from the Dev Tools page
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity
    signal_key       TEXT UNIQUE NOT NULL,
    symbol           TEXT NOT NULL,
    bias             TEXT NOT NULL,             -- LONG | SHORT
    setup_type       TEXT NOT NULL,
    confidence       INTEGER NOT NULL DEFAULT 0,
    source           TEXT NOT NULL DEFAULT 'live',  -- live | sandbox | dev
    thesis           TEXT NOT NULL DEFAULT '',

    -- Entry zone (from trade plan)
    entry_min        REAL NOT NULL,
    entry_max        REAL NOT NULL,
    stop_loss        REAL NOT NULL,
    take_profit_1    REAL NOT NULL,
    take_profit_2    REAL,
    max_hold_minutes INTEGER NOT NULL DEFAULT 60,
    generated_at     TEXT NOT NULL DEFAULT '',  -- trade plan creation time (ISO)
    thesis_anchor    TEXT NOT NULL DEFAULT '',  -- datetime from which max_hold is measured (ISO)

    -- Lifecycle state
    state            TEXT NOT NULL DEFAULT 'PENDING',
    reject_reason    TEXT,

    -- Broker / IB (populated on approval)
    quantity         INTEGER NOT NULL DEFAULT 1,
    entry_limit      REAL,                     -- order limit price sent to IB
    parent_order_id  INTEGER,
    tp_order_id      INTEGER,
    sl_order_id      INTEGER,
    ib_perm_id       INTEGER,

    -- Fill (populated when entry fills)
    entry_fill       REAL,
    fill_time        TEXT,

    -- Exit mode (mutable while FILLED)
    exit_mode        TEXT NOT NULL DEFAULT 'TP1',  -- TP1 | TP2 | MANUAL | HOLD_EXPIRY
    active_tp        REAL,

    -- Close (populated on exit)
    exit_price       REAL,
    exit_time        TEXT,
    exit_reason      TEXT,

    -- Timestamps
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_state ON trades(state);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_source ON trades(source);
"""

_ASSESSMENTS_DDL = """
CREATE TABLE IF NOT EXISTS assessments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id         INTEGER NOT NULL,
    action           TEXT NOT NULL,          -- HOLD | ADJUST | CLOSE
    confidence       INTEGER NOT NULL DEFAULT 0,
    rationale        TEXT NOT NULL DEFAULT '',
    recommendation   TEXT NOT NULL DEFAULT '{}',  -- full JSON blob
    applied          INTEGER NOT NULL DEFAULT 0,  -- 0=pending, 1=applied, -1=dismissed
    created_at       TEXT NOT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_assessments_trade ON assessments(trade_id);
"""


@dataclass
class TradeRow:
    id: int
    signal_key: str
    symbol: str
    bias: str
    setup_type: str
    confidence: int
    source: str
    thesis: str
    entry_min: float
    entry_max: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    max_hold_minutes: int
    generated_at: str
    thesis_anchor: str
    state: str
    reject_reason: str | None
    quantity: int
    entry_limit: float | None
    parent_order_id: int | None
    tp_order_id: int | None
    sl_order_id: int | None
    ib_perm_id: int | None
    entry_fill: float | None
    fill_time: str | None
    exit_mode: str
    active_tp: float | None
    exit_price: float | None
    exit_time: str | None
    exit_reason: str | None
    created_at: str
    updated_at: str


_OPEN_STATES = ("PENDING", "APPROVED", "WORKING", "FILLED")
_ACTIVE_STATES = ("WORKING", "FILLED")


class TradeStore:
    """Thread-compatible SQLite store for the unified trades table.

    Uses ``check_same_thread=False`` because the scheduler writes from a
    background thread while Streamlit reads from the main thread.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.executescript(_ASSESSMENTS_DDL)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that may be missing from older databases."""
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        if "thesis_anchor" not in cols:
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN thesis_anchor TEXT NOT NULL DEFAULT ''"
            )
            # Backfill: set thesis_anchor = generated_at for existing rows
            self._conn.execute(
                "UPDATE trades SET thesis_anchor = generated_at WHERE thesis_anchor = ''"
            )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        signal_key: str,
        symbol: str,
        bias: str,
        setup_type: str,
        entry_min: float,
        entry_max: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float | None,
        thesis: str,
        *,
        confidence: int = 0,
        max_hold_minutes: int = 60,
        generated_at: str = "",
        source: str = "live",
        state: str = "PENDING",
        entry_limit: float | None = None,
        parent_order_id: int | None = None,
        tp_order_id: int | None = None,
        sl_order_id: int | None = None,
        quantity: int = 1,
    ) -> int:
        """Insert a new trade row.  Returns the new row id.

        For pending approvals, call with default ``state='PENDING'``.
        For dev-seeded positions, pass ``state='WORKING'`` or ``state='FILLED'``
        and ``source='dev'`` to skip the approval phase.
        """
        now = datetime.now(tz=timezone.utc).isoformat()
        if not generated_at:
            generated_at = now
        active_tp = take_profit_1 if state in ("WORKING", "FILLED") else None
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO trades
                (signal_key, symbol, bias, setup_type, confidence, source, thesis,
                 entry_min, entry_max, stop_loss, take_profit_1, take_profit_2,
                 max_hold_minutes, generated_at, thesis_anchor,
                 state, quantity, entry_limit,
                 parent_order_id, tp_order_id, sl_order_id,
                 exit_mode, active_tp,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'TP1',?,?,?)
            """,
            (signal_key, symbol, bias, setup_type, confidence, source, thesis,
             entry_min, entry_max, stop_loss, take_profit_1, take_profit_2,
             max_hold_minutes, generated_at, generated_at,
             state, quantity, entry_limit,
             parent_order_id, tp_order_id, sl_order_id,
             active_tp,
             now, now),
        )
        return cur.lastrowid or 0

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def set_state(
        self,
        row_id: int,
        state: str,
        *,
        reject_reason: str | None = None,
    ) -> None:
        """Transition a trade to a new state."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
            SET state=?, reject_reason=COALESCE(?,reject_reason), updated_at=?
            WHERE id=?
            """,
            (state, reject_reason, now, row_id),
        )

    def set_broker_ids(
        self,
        row_id: int,
        *,
        entry_limit: float | None = None,
        parent_order_id: int | None = None,
        tp_order_id: int | None = None,
        sl_order_id: int | None = None,
        ib_perm_id: int | None = None,
    ) -> None:
        """Populate IB order IDs after bracket placement."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
            SET entry_limit=COALESCE(?,entry_limit),
                parent_order_id=COALESCE(?,parent_order_id),
                tp_order_id=COALESCE(?,tp_order_id),
                sl_order_id=COALESCE(?,sl_order_id),
                ib_perm_id=COALESCE(?,ib_perm_id),
                updated_at=?
            WHERE id=?
            """,
            (entry_limit, parent_order_id, tp_order_id, sl_order_id,
             ib_perm_id, now, row_id),
        )

    def mark_filled(
        self, row_id: int, fill_price: float, fill_time: str | None = None,
    ) -> None:
        """Transition WORKING → FILLED."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
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
        """Change exit mode (TP1/TP2/MANUAL/HOLD_EXPIRY) and the active TP price."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
            SET exit_mode=?, active_tp=?,
                tp_order_id=COALESCE(?, tp_order_id),
                updated_at=?
            WHERE id=?
            """,
            (exit_mode, active_tp, tp_order_id, now, row_id),
        )

    def close_trade(
        self,
        row_id: int,
        state: str,
        exit_price: float | None = None,
        exit_reason: str = "",
    ) -> None:
        """Mark a trade as closed (terminal state)."""
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
            SET state=?, exit_price=?, exit_time=?, exit_reason=?, updated_at=?
            WHERE id=?
            """,
            (state, exit_price, now, exit_reason, now, row_id),
        )

    def expire_stale(self, expiry_minutes: int) -> int:
        """Mark PENDING trades older than *expiry_minutes* as EXPIRED.

        Returns the number of rows updated.
        """
        cutoff = (
            datetime.now(tz=timezone.utc).timestamp() - expiry_minutes * 60
        )
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            UPDATE trades
            SET state='EXPIRED', updated_at=?
            WHERE state='PENDING'
              AND unixepoch(created_at) < ?
            """,
            (now, cutoff),
        )
        return cur.rowcount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_pending(self) -> list[TradeRow]:
        """Return all PENDING trades, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE state='PENDING' ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_active(self) -> list[TradeRow]:
        """Return WORKING + FILLED trades."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE state IN ('WORKING','FILLED') ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_filled(self) -> list[TradeRow]:
        """Return only FILLED trades."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE state='FILLED' ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_working(self) -> list[TradeRow]:
        """Return only WORKING trades."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE state='WORKING' ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_open(self) -> list[TradeRow]:
        """Return all non-terminal trades (PENDING through FILLED)."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE state IN ('PENDING','APPROVED','WORKING','FILLED') "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def has_active_trade(self) -> bool:
        """Check if any trade is WORKING or FILLED (duplicate guard)."""
        r = self._conn.execute(
            "SELECT 1 FROM trades WHERE state IN ('WORKING','FILLED') LIMIT 1"
        ).fetchone()
        return r is not None

    def get_by_id(self, row_id: int) -> TradeRow | None:
        r = self._conn.execute(
            "SELECT * FROM trades WHERE id=?", (row_id,)
        ).fetchone()
        return _row(r) if r else None

    def get_all(self, limit: int = 100) -> list[TradeRow]:
        """Return recent trades of any state, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row(r) for r in rows]

    def get_history(self, limit: int = 50) -> list[TradeRow]:
        """Return closed/rejected/expired trades, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE state NOT IN ('PENDING','APPROVED','WORKING','FILLED') "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Trade parameter updates (for assessment apply)
    # ------------------------------------------------------------------

    def update_stop_loss(self, row_id: int, new_sl: float, sl_order_id: int | None = None) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
            SET stop_loss=?, sl_order_id=COALESCE(?,sl_order_id), updated_at=?
            WHERE id=?
            """,
            (new_sl, sl_order_id, now, row_id),
        )

    def update_take_profits(
        self,
        row_id: int,
        tp1: float | None = None,
        tp2: float | None = None,
        tp_order_id: int | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
            SET take_profit_1=COALESCE(?,take_profit_1),
                take_profit_2=COALESCE(?,take_profit_2),
                tp_order_id=COALESCE(?,tp_order_id),
                updated_at=?
            WHERE id=?
            """,
            (tp1, tp2, tp_order_id, now, row_id),
        )

    def update_entry_limit(
        self,
        row_id: int,
        new_entry: float,
        parent_order_id: int | None = None,
        tp_order_id: int | None = None,
        sl_order_id: int | None = None,
        ib_perm_id: int | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE trades
            SET entry_limit=?,
                parent_order_id=COALESCE(?,parent_order_id),
                tp_order_id=COALESCE(?,tp_order_id),
                sl_order_id=COALESCE(?,sl_order_id),
                ib_perm_id=COALESCE(?,ib_perm_id),
                updated_at=?
            WHERE id=? AND state='WORKING'
            """,
            (new_entry, parent_order_id, tp_order_id, sl_order_id, ib_perm_id, now, row_id),
        )

    def update_hold_window(self, row_id: int, new_minutes: int) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE trades SET max_hold_minutes=?, thesis_anchor=?, updated_at=? WHERE id=?",
            (new_minutes, now, now, row_id),
        )

    # ------------------------------------------------------------------
    # Assessments log
    # ------------------------------------------------------------------

    def log_assessment(
        self,
        trade_id: int,
        action: str,
        confidence: int,
        rationale: str,
        recommendation_json: str,
    ) -> int:
        """Store an assessment record.  Returns the assessment id."""
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO assessments (trade_id, action, confidence, rationale, recommendation, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (trade_id, action, confidence, rationale, recommendation_json, now),
        )
        return cur.lastrowid or 0

    def mark_assessment_applied(self, assessment_id: int, applied: int = 1) -> None:
        """Mark an assessment as applied (1) or dismissed (-1)."""
        self._conn.execute(
            "UPDATE assessments SET applied=? WHERE id=?",
            (applied, assessment_id),
        )

    def get_assessments(self, trade_id: int, limit: int = 10) -> list[dict]:
        """Return recent assessments for a trade, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM assessments WHERE trade_id=? ORDER BY created_at DESC LIMIT ?",
            (trade_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _row(r: sqlite3.Row) -> TradeRow:
    return TradeRow(
        id=r["id"],
        signal_key=r["signal_key"],
        symbol=r["symbol"],
        bias=r["bias"],
        setup_type=r["setup_type"],
        confidence=r["confidence"],
        source=r["source"],
        thesis=r["thesis"],
        entry_min=r["entry_min"],
        entry_max=r["entry_max"],
        stop_loss=r["stop_loss"],
        take_profit_1=r["take_profit_1"],
        take_profit_2=r["take_profit_2"],
        max_hold_minutes=r["max_hold_minutes"],
        generated_at=r["generated_at"],
        thesis_anchor=r["thesis_anchor"],
        state=r["state"],
        reject_reason=r["reject_reason"],
        quantity=r["quantity"],
        entry_limit=r["entry_limit"],
        parent_order_id=r["parent_order_id"],
        tp_order_id=r["tp_order_id"],
        sl_order_id=r["sl_order_id"],
        ib_perm_id=r["ib_perm_id"],
        entry_fill=r["entry_fill"],
        fill_time=r["fill_time"],
        exit_mode=r["exit_mode"],
        active_tp=r["active_tp"],
        exit_price=r["exit_price"],
        exit_time=r["exit_time"],
        exit_reason=r["exit_reason"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )
