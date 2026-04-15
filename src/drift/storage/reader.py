"""EventReader — load persisted SignalEvents from the JSONL event log."""
from __future__ import annotations

import json
from pathlib import Path

from drift.models import SignalEvent


def load_events_from_log(path: Path | str) -> list[SignalEvent]:
    """Return all SignalEvents stored in *path* (JSONL format).

    Malformed or blank lines are silently skipped so the reader is tolerant
    of partially-written records at the tail of the file.
    """
    path = Path(path)
    if not path.exists():
        return []

    events: list[SignalEvent] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(SignalEvent.model_validate(json.loads(line)))
            except Exception:
                continue

    return events
