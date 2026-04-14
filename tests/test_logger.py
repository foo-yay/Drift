import json
from datetime import datetime, timezone

from drift.models import SignalEvent
from drift.storage.logger import EventLogger


def test_event_logger_writes_jsonl(tmp_path) -> None:
    log_path = tmp_path / "events.jsonl"
    logger = EventLogger(log_path)

    logger.append_event(
        SignalEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol="MNQ",
            final_outcome="DRY_RUN",
            final_reason="test event",
        )
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["symbol"] == "MNQ"
    assert payload["final_outcome"] == "DRY_RUN"
