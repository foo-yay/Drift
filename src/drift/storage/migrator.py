"""One-shot JSONL → SQLite migration utility.

Reads an existing ``events.jsonl`` audit log and imports every entry into a
``SignalStore``.  The operation is fully **idempotent** — running it twice on
the same file is safe because the underlying store uses ``INSERT OR IGNORE``.

Usage (from CLI or startup hook)
---------------------------------
>>> from drift.storage.migrator import migrate_jsonl
>>> migrated, skipped, errors = migrate_jsonl("logs/events.jsonl", "data/local.db")
>>> print(f"Imported {migrated}, skipped {skipped} duplicates, {errors} errors")
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

from drift.models import SignalEvent
from drift.storage.signal_store import SignalStore


class MigrationResult(NamedTuple):
    migrated: int
    skipped: int
    errors: int


def migrate_jsonl(
    jsonl_path: str | Path,
    db_path: str | Path,
) -> MigrationResult:
    """Import all events from *jsonl_path* into the SQLite store at *db_path*.

    Lines that fail JSON parsing or ``SignalEvent`` validation are counted as
    errors and skipped rather than raising — so a corrupt line cannot abort the
    entire migration.

    Returns:
        ``MigrationResult(migrated, skipped, errors)``
    """
    jsonl_path = Path(jsonl_path)
    if not jsonl_path.exists():
        return MigrationResult(0, 0, 0)

    store = SignalStore(db_path)
    migrated = 0
    skipped = 0
    errors = 0

    try:
        with jsonl_path.open(encoding="utf-8") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    event = SignalEvent.model_validate(data)
                    inserted = store.insert_event(event)
                    if inserted:
                        migrated += 1
                    else:
                        skipped += 1
                except (json.JSONDecodeError, ValueError) as exc:
                    errors += 1
                    # Non-fatal — log and continue
                    import sys  # noqa: PLC0415

                    print(
                        f"[migrator] line {lineno}: skipping — {exc}",
                        file=sys.stderr,
                    )
    finally:
        store.close()

    return MigrationResult(migrated, skipped, errors)
