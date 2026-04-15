from __future__ import annotations

import json
from pathlib import Path

from drift.models import SignalEvent
from drift.storage.signal_store import SignalStore


class EventLogger:
    """Dual-write event logger — JSONL (audit) + SQLite (query layer).

    Args:
        jsonl_path: Append-only JSONL audit log.
        sqlite_path: SQLite database managed by :class:`~drift.storage.signal_store.SignalStore`.
                     Pass ``None`` to disable SQLite writes (logging-only mode).
    """

    def __init__(self, jsonl_path: str | Path, sqlite_path: str | Path | None = None) -> None:
        self.path = Path(jsonl_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._store = SignalStore(sqlite_path) if sqlite_path else None

    def append_event(self, event: SignalEvent) -> None:
        event = event.ensure_signal_key()
        with self.path.open("a", encoding="utf-8") as file_handle:
            file_handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True))
            file_handle.write("\n")
        if self._store is not None:
            self._store.insert_event(event)

