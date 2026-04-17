"""Pending order store — tracks trade plans awaiting operator approval.

When Drift issues a TRADE_PLAN_ISSUED signal and the broker integration is
enabled, a pending order row is created here.  The Streamlit Orders page reads
this table to show approval cards.  On approval the GUI calls ``approve()``
which sets the state to SUBMITTING; the IB client then updates it to SUBMITTED
or FAILED.

States:
    PENDING    — awaiting operator approval
    APPROVED   — operator approved; order submission in progress
    SUBMITTED  — bracket order accepted by IB Gateway
    REJECTED   — operator manually rejected
    EXPIRED    — approval window elapsed without action
    FAILED     — IB rejected or connection error
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS pending_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key      TEXT UNIQUE NOT NULL,
    symbol          TEXT NOT NULL,
    bias            TEXT NOT NULL,           -- LONG | SHORT
    setup_type      TEXT NOT NULL,
    confidence      INTEGER NOT NULL,
    entry_min       REAL NOT NULL,
    entry_max       REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    take_profit_1   REAL NOT NULL,
    take_profit_2   REAL,
    thesis          TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'PENDING',
    ib_order_id     INTEGER,                 -- filled after submission
    ib_perm_id      INTEGER,
    reject_reason   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_po_state ON pending_orders(state);
CREATE INDEX IF NOT EXISTS idx_po_created ON pending_orders(created_at);
"""


@dataclass
class PendingOrderRow:
    id: int
    signal_key: str
    symbol: str
    bias: str
    setup_type: str
    confidence: int
    entry_min: float
    entry_max: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    thesis: str
    state: str
    ib_order_id: int | None
    ib_perm_id: int | None
    reject_reason: str | None
    created_at: str
    updated_at: str


class PendingOrderStore:
    """Thread-compatible SQLite store for pending IB orders.

    Uses ``check_same_thread=False`` because the scheduler writes from a
    background thread while Streamlit reads from the main thread.  Individual
    writes are serialised by SQLite's internal locking.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create(
        self,
        signal_key: str,
        symbol: str,
        bias: str,
        setup_type: str,
        confidence: int,
        entry_min: float,
        entry_max: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float | None,
        thesis: str,
    ) -> int:
        """Insert a new PENDING order.  Returns the new row id."""
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO pending_orders
                (signal_key, symbol, bias, setup_type, confidence,
                 entry_min, entry_max, stop_loss, take_profit_1, take_profit_2,
                 thesis, state, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?)
            """,
            (signal_key, symbol, bias, setup_type, confidence,
             entry_min, entry_max, stop_loss, take_profit_1, take_profit_2,
             thesis, now, now),
        )
        return cur.lastrowid or 0

    def set_state(
        self,
        row_id: int,
        state: str,
        *,
        ib_order_id: int | None = None,
        ib_perm_id: int | None = None,
        reject_reason: str | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._conn.execute(
            """
            UPDATE pending_orders
            SET state=?, ib_order_id=COALESCE(?,ib_order_id),
                ib_perm_id=COALESCE(?,ib_perm_id),
                reject_reason=COALESCE(?,reject_reason),
                updated_at=?
            WHERE id=?
            """,
            (state, ib_order_id, ib_perm_id, reject_reason, now, row_id),
        )

    def expire_stale(self, expiry_minutes: int) -> int:
        """Mark PENDING orders older than expiry_minutes as EXPIRED.

        Returns the number of rows updated.
        """
        cutoff = (
            datetime.now(tz=timezone.utc).timestamp() - expiry_minutes * 60
        )
        # SQLite stores ISO strings; compare via unixepoch()
        cur = self._conn.execute(
            """
            UPDATE pending_orders
            SET state='EXPIRED', updated_at=?
            WHERE state='PENDING'
              AND unixepoch(created_at) < ?
            """,
            (datetime.now(tz=timezone.utc).isoformat(), cutoff),
        )
        return cur.rowcount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_pending(self) -> list[PendingOrderRow]:
        """Return all PENDING orders, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM pending_orders WHERE state='PENDING' ORDER BY created_at ASC"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_all(self, limit: int = 100) -> list[PendingOrderRow]:
        """Return recent orders of any state, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM pending_orders ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row(r) for r in rows]

    def get_by_id(self, row_id: int) -> PendingOrderRow | None:
        r = self._conn.execute(
            "SELECT * FROM pending_orders WHERE id=?", (row_id,)
        ).fetchone()
        return _row(r) if r else None

    def close(self) -> None:
        self._conn.close()


def _row(r: sqlite3.Row) -> PendingOrderRow:
    return PendingOrderRow(
        id=r["id"],
        signal_key=r["signal_key"],
        symbol=r["symbol"],
        bias=r["bias"],
        setup_type=r["setup_type"],
        confidence=r["confidence"],
        entry_min=r["entry_min"],
        entry_max=r["entry_max"],
        stop_loss=r["stop_loss"],
        take_profit_1=r["take_profit_1"],
        take_profit_2=r["take_profit_2"],
        thesis=r["thesis"],
        state=r["state"],
        ib_order_id=r["ib_order_id"],
        ib_perm_id=r["ib_perm_id"],
        reject_reason=r["reject_reason"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )
