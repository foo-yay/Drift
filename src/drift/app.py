from __future__ import annotations

from datetime import datetime, timezone
from time import sleep

from drift.config.models import AppConfig
from drift.data.providers.yfinance_provider import YFinanceProvider
from drift.features.engine import FeatureEngine
from drift.gates.calendar_gate import CalendarGate
from drift.models import MarketSnapshot, SignalEvent
from drift.output.console import render_gate_blocked, render_gate_result, render_snapshot, render_startup, render_status, render_success
from drift.storage.logger import EventLogger


class DriftApplication:
    def __init__(self, config: AppConfig, config_path: str) -> None:
        self.config = config
        self.config_path = config_path
        self.event_logger = EventLogger(config.storage.jsonl_event_log)
        self._provider = YFinanceProvider()
        self._engine = FeatureEngine(config)
        self._calendar_gate = CalendarGate(config.calendar)

    def run_once(self) -> None:
        render_startup(self.config, self.config_path)

        symbol = self.config.instrument.symbol

        # ------------------------------------------------------------------
        # Fetch market data
        # ------------------------------------------------------------------
        render_status("fetching market data...")
        try:
            last_price = self._provider.get_latest_quote(symbol)
        except ValueError as exc:
            render_status(f"[red]data error:[/red] {exc} — skipping cycle")
            return

        session = self._provider.get_session_status(symbol)
        bars_1m = self._provider.get_recent_bars(symbol, "1m", self.config.lookbacks.bars_1m)
        bars_5m = self._provider.get_recent_bars(symbol, "5m", self.config.lookbacks.bars_5m)
        bars_1h = self._provider.get_recent_bars(symbol, "1h", self.config.lookbacks.bars_1h)

        render_success(
            f"data fetched — {len(bars_1m)}×1m  {len(bars_5m)}×5m  {len(bars_1h)}×1h  "
            f"quote={last_price:,.2f}  session={session}"
        )

        # ------------------------------------------------------------------
        # Compute features → MarketSnapshot
        # ------------------------------------------------------------------
        render_status("computing features...")
        snapshot: MarketSnapshot = self._engine.compute(
            bars_1m=bars_1m,
            bars_5m=bars_5m,
            bars_1h=bars_1h,
            last_price=last_price,
            session=session,
        )
        render_snapshot(snapshot)

        # ------------------------------------------------------------------
        # Gate layer
        # ------------------------------------------------------------------
        render_status("evaluating gates...")
        calendar_result = self._calendar_gate.evaluate(snapshot)
        render_gate_result(calendar_result)

        if not calendar_result.passed:
            render_gate_blocked(calendar_result)
            event = SignalEvent(
                event_time=datetime.now(tz=timezone.utc),
                symbol=symbol,
                snapshot=snapshot.model_dump(mode="json"),
                final_outcome="BLOCKED",
                final_reason=calendar_result.reason,
            )
            self.event_logger.append_event(event)
            render_success(f"blocked cycle logged to {self.config.storage.jsonl_event_log}")
            return

        # ------------------------------------------------------------------
        # Log the cycle event (all gates passed)
        # ------------------------------------------------------------------
        event = SignalEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol=symbol,
            snapshot=snapshot.model_dump(mode="json"),
            final_outcome="SNAPSHOT_ONLY",
            final_reason="Gates passed. LLM layer not yet wired.",
        )
        self.event_logger.append_event(event)
        render_success(f"cycle logged to {self.config.storage.jsonl_event_log}")

    def run_forever(self) -> None:
        while True:
            self.run_once()
            sleep(self.config.app.loop_interval_seconds)


