"""SQLite-backed signal store — the query layer for the Drift GUI.

All signal reads performed by the GUI go through this module, not through
the JSONL event log.  The JSONL log is retained as an immutable append-only
audit trail; SQLite is the indexed, queryable projection.

Architecture notes
------------------
- ``SignalStore`` holds a single ``sqlite3.Connection`` and is not thread-safe by
  itself.  In Streamlit's single-threaded execution model this is fine.
- The schema is created on ``__init__`` (idempotent ``CREATE TABLE IF NOT EXISTS``).
- All writes use ``INSERT OR IGNORE`` on ``signal_key`` — duplicate events are
  silently dropped.  This is the core deduplication guarantee.
- ``upsert_outcome()`` updates ``pnl_points`` and ``replay_outcome`` in-place after
  backfill resolves a pending signal.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from drift.models import SignalEvent

_DDL = """
CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key       TEXT UNIQUE NOT NULL,
    symbol           TEXT NOT NULL,
    source           TEXT NOT NULL,
    event_time_utc   TEXT NOT NULL,
    as_of_utc        TEXT,
    final_outcome    TEXT NOT NULL,
    bias             TEXT,
    setup_type       TEXT,
    confidence       INTEGER,
    entry_min        REAL,
    entry_max        REAL,
    stop_loss        REAL,
    take_profit_1    REAL,
    take_profit_2    REAL,
    reward_risk      REAL,
    pnl_points       REAL,
    replay_outcome   TEXT,
    thesis           TEXT,
    snapshot_json    TEXT,
    gate_report_json TEXT,
    llm_json         TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_date ON signals(symbol, as_of_utc);
CREATE INDEX IF NOT EXISTS idx_signals_source      ON signals(source);
CREATE INDEX IF NOT EXISTS idx_signals_outcome     ON signals(final_outcome);

CREATE TABLE IF NOT EXISTS replay_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key       TEXT UNIQUE NOT NULL,
    symbol        TEXT NOT NULL,
    date_start    TEXT NOT NULL,
    date_end      TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'replay',
    signal_count  INTEGER DEFAULT 0,
    completed_at  TEXT NOT NULL
);
"""


@dataclass
class SignalRow:
    """Flat projection of a signal record returned by query methods."""

    id: int
    signal_key: str
    symbol: str
    source: str
    event_time_utc: str
    as_of_utc: str | None
    final_outcome: str
    bias: str | None
    setup_type: str | None
    confidence: int | None
    entry_min: float | None
    entry_max: float | None
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    reward_risk: float | None
    pnl_points: float | None
    replay_outcome: str | None
    thesis: str | None
    snapshot_json: str | None
    gate_report_json: str | None
    llm_json: str | None
    created_at: str

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def snapshot(self) -> dict[str, Any] | None:
        return json.loads(self.snapshot_json) if self.snapshot_json else None

    @property
    def gate_report(self) -> dict[str, Any] | None:
        return json.loads(self.gate_report_json) if self.gate_report_json else None

    @property
    def llm_decision(self) -> dict[str, Any] | None:
        return json.loads(self.llm_json) if self.llm_json else None

    @property
    def event_time(self) -> datetime:
        return datetime.fromisoformat(self.event_time_utc)

    @property
    def is_trade_plan(self) -> bool:
        return self.final_outcome == "TRADE_PLAN_ISSUED"

    @property
    def is_resolved(self) -> bool:
        return self.replay_outcome is not None


class SignalStore:
    """SQLite signal store.

    Args:
        db_path: Path to the ``.db`` file.  Created (with parent dirs) if it
                 does not exist.  Pass ``":memory:"`` for unit tests.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode lets the GUI's read connection see committed rows from the
        # scheduler's write connection without either blocking the other.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def insert_event(self, event: SignalEvent) -> bool:
        """Insert a signal event.  Returns True if inserted, False if skipped (duplicate)."""
        event = event.ensure_signal_key()
        tp = event.trade_plan or {}
        plan_llm = event.llm_decision_parsed or {}

        bias        = tp.get("bias") or plan_llm.get("decision")
        setup_type  = tp.get("setup_type") or plan_llm.get("setup_type")
        confidence  = tp.get("confidence") or plan_llm.get("confidence")
        entry_min   = tp.get("entry_min")
        entry_max   = tp.get("entry_max")
        stop_loss   = tp.get("stop_loss")
        tp1         = tp.get("take_profit_1")
        tp2         = tp.get("take_profit_2")
        rr          = tp.get("reward_risk_ratio")
        thesis      = tp.get("thesis") or plan_llm.get("thesis")
        pnl         = (event.replay_outcome or {}).get("pnl_points")
        ro_label    = (event.replay_outcome or {}).get("outcome")
        as_of       = (event.snapshot or {}).get("as_of")

        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO signals (
                signal_key, symbol, source, event_time_utc, as_of_utc,
                final_outcome, bias, setup_type, confidence,
                entry_min, entry_max, stop_loss,
                take_profit_1, take_profit_2, reward_risk,
                pnl_points, replay_outcome, thesis,
                snapshot_json, gate_report_json, llm_json,
                created_at
            ) VALUES (
                ?,?,?,?,?,
                ?,?,?,?,
                ?,?,?,
                ?,?,?,
                ?,?,?,
                ?,?,?,
                ?
            )
            """,
            (
                event.signal_key,
                event.symbol,
                event.source,
                event.event_time.isoformat(),
                as_of,
                event.final_outcome,
                bias,
                setup_type,
                confidence,
                entry_min,
                entry_max,
                stop_loss,
                tp1,
                tp2,
                rr,
                pnl,
                ro_label,
                thesis,
                json.dumps(event.snapshot) if event.snapshot else None,
                json.dumps(event.pre_gate_report) if event.pre_gate_report else None,
                json.dumps(event.llm_decision_parsed) if event.llm_decision_parsed else None,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def upsert_outcome(self, signal_key: str, outcome_label: str, pnl_points: float) -> None:
        """Update the outcome fields for a resolved signal (called by backfill)."""
        self._conn.execute(
            "UPDATE signals SET replay_outcome=?, pnl_points=? WHERE signal_key=?",
            (outcome_label, pnl_points, signal_key),
        )
        self._conn.commit()

    def resolve_live_signal(
        self, signal_id: int, outcome: str, pnl_points: float = 0.0
    ) -> None:
        """Write an outcome for a live pending trade plan (called by the scheduler watch loop).

        Outcome labels:
        - ``TP1_HIT``        — entry zone was touched; price reached take-profit 1
        - ``TP2_HIT``        — entry zone was touched; price reached take-profit 2
        - ``STOP_HIT``       — entry zone was touched; price hit the stop loss
        - ``ENTRY_MISSED``   — TP/SL triggered but entry zone was never touched; no fill
        - ``EXPIRED``        — time horizon elapsed; entry zone was touched (position open at expiry)
        - ``EXPIRED_NO_FILL``— time horizon elapsed; entry zone never touched; no fill
        """
        self._conn.execute(
            "UPDATE signals SET replay_outcome=?, pnl_points=? WHERE id=?",
            (outcome, pnl_points, signal_id),
        )
        self._conn.commit()

    def delete_by_key(self, signal_key: str) -> None:
        """Delete a single signal by key (user-initiated from the GUI)."""
        self._conn.execute("DELETE FROM signals WHERE signal_key=?", (signal_key,))
        self._conn.commit()

    def delete_by_date_range(
        self,
        symbol: str,
        date_start: date,
        date_end: date,
        source: str = "replay",
    ) -> int:
        """Delete all signals for a symbol/date range/source (used by Replay Lab overwrite).

        Returns the number of rows deleted.
        """
        cur = self._conn.execute(
            """
            DELETE FROM signals
            WHERE symbol=?
              AND source=?
              AND DATE(as_of_utc) BETWEEN ? AND ?
            """,
            (symbol, source, date_start.isoformat(), date_end.isoformat()),
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def count_by_date_range(
        self,
        symbol: str,
        date_start: date,
        date_end: date,
        source: str = "replay",
    ) -> int:
        """Return how many signals exist for a date range (used by Replay Lab dedup check)."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) FROM signals
            WHERE symbol=?
              AND source=?
              AND DATE(as_of_utc) BETWEEN ? AND ?
            """,
            (symbol, source, date_start.isoformat(), date_end.isoformat()),
        ).fetchone()
        return int(row[0])

    def query(
        self,
        *,
        symbol: str | None = None,
        sources: list[str] | None = None,
        outcomes: list[str] | None = None,
        date_start: date | None = None,
        date_end: date | None = None,
        trade_plans_only: bool = False,
        limit: int = 500,
        offset: int = 0,
        order_desc: bool = True,
    ) -> list[SignalRow]:
        """Flexible filtered query.  All filters are optional (AND-combined)."""
        clauses: list[str] = []
        params: list[Any] = []

        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if sources:
            placeholders = ",".join("?" * len(sources))
            clauses.append(f"source IN ({placeholders})")
            params.extend(sources)
        if outcomes:
            placeholders = ",".join("?" * len(outcomes))
            clauses.append(f"final_outcome IN ({placeholders})")
            params.extend(outcomes)
        if date_start:
            clauses.append("DATE(as_of_utc) >= ?")
            params.append(date_start.isoformat())
        if date_end:
            clauses.append("DATE(as_of_utc) <= ?")
            params.append(date_end.isoformat())
        if trade_plans_only:
            clauses.append("final_outcome = 'TRADE_PLAN_ISSUED'")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if order_desc else "ASC"
        params.extend([limit, offset])

        rows = self._conn.execute(
            f"SELECT * FROM signals {where} ORDER BY event_time_utc {order} LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_row_to_signal(r) for r in rows]

    def get_by_key(self, signal_key: str) -> SignalRow | None:
        row = self._conn.execute(
            "SELECT * FROM signals WHERE signal_key=?", (signal_key,)
        ).fetchone()
        return _row_to_signal(row) if row else None

    def get_pending_live_signals(self, symbol: str) -> list[SignalRow]:
        """Return unresolved TRADE_PLAN_ISSUED live signals for backfill."""
        rows = self._conn.execute(
            """
            SELECT * FROM signals
            WHERE symbol=? AND source='live'
              AND final_outcome='TRADE_PLAN_ISSUED'
              AND replay_outcome IS NULL
            ORDER BY event_time_utc ASC
            """,
            (symbol,),
        ).fetchall()
        return [_row_to_signal(r) for r in rows]

    # ------------------------------------------------------------------
    # Replay run tracking
    # ------------------------------------------------------------------

    def record_replay_run(
        self,
        symbol: str,
        date_start: date,
        date_end: date,
        signal_count: int,
        source: str = "replay",
    ) -> None:
        """Record a completed replay run for the Replay Lab history panel."""
        import hashlib  # noqa: PLC0415

        run_key = hashlib.sha256(
            f"{symbol}|{date_start}|{date_end}|{source}".encode()
        ).hexdigest()[:16]
        self._conn.execute(
            """
            INSERT OR REPLACE INTO replay_runs
                (run_key, symbol, date_start, date_end, source, signal_count, completed_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                run_key,
                symbol,
                date_start.isoformat(),
                date_end.isoformat(),
                source,
                signal_count,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Aggregate stats (used by GUI metric row)
    # ------------------------------------------------------------------

    def win_rate_and_pnl(
        self,
        *,
        sources: list[str] | None = None,
        date_start: date | None = None,
        date_end: date | None = None,
    ) -> dict[str, Any]:
        """Return {total, resolved, wins, win_rate_pct, total_pnl} for trade plans."""
        rows = self.query(
            sources=sources,
            date_start=date_start,
            date_end=date_end,
            trade_plans_only=True,
            limit=100_000,
        )
        resolved = [r for r in rows if r.replay_outcome is not None]
        wins = [r for r in resolved if r.replay_outcome in ("TP1_HIT", "TP2_HIT")]
        total_pnl = sum(r.pnl_points or 0.0 for r in resolved)
        decisive = [r for r in resolved if r.replay_outcome in ("TP1_HIT", "TP2_HIT", "STOP_HIT")]
        win_rate = round(len(wins) / len(decisive) * 100, 1) if decisive else 0.0
        return {
            "total": len(rows),
            "resolved": len(resolved),
            "wins": len(wins),
            "win_rate_pct": win_rate,
            "total_pnl": round(total_pnl, 2),
        }

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_signal(row: sqlite3.Row) -> SignalRow:
    return SignalRow(
        id=row["id"],
        signal_key=row["signal_key"],
        symbol=row["symbol"],
        source=row["source"],
        event_time_utc=row["event_time_utc"],
        as_of_utc=row["as_of_utc"],
        final_outcome=row["final_outcome"],
        bias=row["bias"],
        setup_type=row["setup_type"],
        confidence=row["confidence"],
        entry_min=row["entry_min"],
        entry_max=row["entry_max"],
        stop_loss=row["stop_loss"],
        take_profit_1=row["take_profit_1"],
        take_profit_2=row["take_profit_2"],
        reward_risk=row["reward_risk"],
        pnl_points=row["pnl_points"],
        replay_outcome=row["replay_outcome"],
        thesis=row["thesis"],
        snapshot_json=row["snapshot_json"],
        gate_report_json=row["gate_report_json"],
        llm_json=row["llm_json"],
        created_at=row["created_at"],
    )
