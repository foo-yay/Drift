from __future__ import annotations

from datetime import datetime, timezone
from time import sleep

from drift.config.models import AppConfig
from drift.models import SignalEvent
from drift.output.console import render_startup, render_status, render_success
from drift.storage.logger import EventLogger


class DriftApplication:
    def __init__(self, config: AppConfig, config_path: str) -> None:
        self.config = config
        self.config_path = config_path
        self.event_logger = EventLogger(config.storage.jsonl_event_log)

    def validate_runtime(self) -> None:
        if self.config.app.mode != "dry-run":
            msg = (
                "Only dry-run mode is wired in the initial scaffold. "
                "Configure a provider, feature pipeline, and LLM integration before using "
                f"{self.config.app.mode}."
            )
            raise NotImplementedError(msg)

    def run_once(self) -> None:
        self.validate_runtime()
        render_startup(self.config, self.config_path)
        render_status("validated configuration and initialized dry-run scaffold")

        event = SignalEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol=self.config.instrument.symbol,
            final_outcome="DRY_RUN",
            final_reason="Initial project scaffold validated successfully.",
        )
        self.event_logger.append_event(event)
        render_success(f"logged dry-run lifecycle event to {self.config.storage.jsonl_event_log}")

    def run_forever(self) -> None:
        while True:
            self.run_once()
            sleep(self.config.app.loop_interval_seconds)

