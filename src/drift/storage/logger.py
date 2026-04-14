from __future__ import annotations

import json
from pathlib import Path

from drift.models import SignalEvent


class EventLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_event(self, event: SignalEvent) -> None:
        with self.path.open("a", encoding="utf-8") as file_handle:
            file_handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True))
            file_handle.write("\n")

