"""SQLite-backed watch condition store.

Watches are price/indicator levels the LLM sets during a NO_TRADE cycle.
The fast-poll watcher checks them every ~30 seconds and fires a full LLM
cycle when any condition is met.

Schema notes
------------
- One row per active watch condition.
- ``source_signal_key`` links back to the NO_TRADE signal that created it.
- ``triggered_at`` is NULL until the condition fires; once set, the watch is
  considered consumed and ignored by the watcher.
- Watches auto-expire at ``expires_at`` (computed from ``expires_minutes`` on
  the ``WatchCondition`` model).
- All watches for a symbol are cleared whenever a new NO_TRADE cycle runs for
  that symbol, so stale conditions from a previous assessment do not linger.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drift.models import WatchCondition

_DDL = """
CREATE TABLE IF NOT EXISTS watches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    source_signal_key TEXT,
    condition_type    TEXT NOT NULL,
    value             REAL NOT NULL,
    description       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    triggered_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_watches_symbol ON watches(symbol);
CREATE INDEX IF NOT EXISTS idx_watches_active  ON watches(symbol, triggered_at, expires_at);
"""


@dataclass
class WatchRow:
    id: int
    symbol: str
    source_signal_key: str | None
    condition_type: str
    value: float
    description: str
    created_at: str
    expires_at: str
    triggered_at: str | None

    @property
    def is_active(self) -> bool:
        """True if not yet triggered and not expired."""
        if self.triggered_at is not None:
            return False
        return datetime.fromisoformat(self.expires_at) > datetime.now(tz=timezone.utc)


class WatchStore:
    """Manages active watch conditions in a SQLite database.

    Designed to share the same ``.db`` file as ``SignalStore`` — just pass the
    same ``db_path``.  The schema is created idempotently on ``__init__``.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def replace_watches(
        self,
        symbol: str,
        conditions: list[WatchCondition],
        source_signal_key: str | None = None,
    ) -> None:
        """Clear all existing watches for *symbol* and insert the new set.

        Called after each NO_TRADE cycle so stale conditions are replaced with
        the LLM's latest assessment.  A cycle with an empty ``conditions`` list
        simply clears existing watches.
        """
        now = datetime.now(tz=timezone.utc)
        self._conn.execute("DELETE FROM watches WHERE symbol=?", (symbol,))
        for cond in conditions:
            expires_at = now + timedelta(minutes=cond.expires_minutes)
            self._conn.execute(
                """
                INSERT INTO watches
                    (symbol, source_signal_key, condition_type, value, description,
                     created_at, expires_at, triggered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    symbol,
                    source_signal_key,
                    cond.condition_type,
                    cond.value,
                    cond.description,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        self._conn.commit()

    def mark_triggered(self, watch_id: int) -> None:
        """Mark a single watch as triggered (consumed by the watcher)."""
        self._conn.execute(
            "UPDATE watches SET triggered_at=? WHERE id=?",
            (datetime.now(tz=timezone.utc).isoformat(), watch_id),
        )
        self._conn.commit()

    def clear_expired(self, symbol: str) -> int:
        """Remove expired, untriggered watches. Returns count deleted."""
        now = datetime.now(tz=timezone.utc).isoformat()
        cur = self._conn.execute(
            "DELETE FROM watches WHERE symbol=? AND triggered_at IS NULL AND expires_at <= ?",
            (symbol, now),
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_active(self, symbol: str) -> list[WatchRow]:
        """Return all non-triggered, non-expired watches for *symbol*."""
        now = datetime.now(tz=timezone.utc).isoformat()
        rows = self._conn.execute(
            """
            SELECT * FROM watches
            WHERE symbol=?
              AND triggered_at IS NULL
              AND expires_at > ?
            ORDER BY id ASC
            """,
            (symbol, now),
        ).fetchall()
        return [_row_to_watch(r) for r in rows]

    def get_all(self, symbol: str, limit: int = 50) -> list[WatchRow]:
        """Return all watches (active + triggered + expired) for display."""
        rows = self._conn.execute(
            "SELECT * FROM watches WHERE symbol=? ORDER BY id DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [_row_to_watch(r) for r in rows]


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _row_to_watch(row: sqlite3.Row) -> WatchRow:
    return WatchRow(
        id=row["id"],
        symbol=row["symbol"],
        source_signal_key=row["source_signal_key"],
        condition_type=row["condition_type"],
        value=row["value"],
        description=row["description"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        triggered_at=row["triggered_at"],
    )
