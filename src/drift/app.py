from __future__ import annotations

from datetime import datetime, timezone
from time import sleep

from drift.config.models import AppConfig
from drift.data.providers.yfinance_provider import YFinanceProvider
from drift.features.engine import FeatureEngine
from drift.gates.calendar_gate import CalendarGate
from drift.gates.cooldown_gate import CooldownGate
from drift.gates.kill_switch_gate import KillSwitchGate
from drift.gates.regime_gate import RegimeGate
from drift.gates.runner import GateRunner
from drift.gates.session_gate import SessionGate
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
        self._gate_runner = GateRunner([
            KillSwitchGate(config.gates),
            SessionGate(config.sessions),
            CalendarGate(config.calendar),
            RegimeGate(config.gates),
            CooldownGate(config.gates, config.risk, config.storage.jsonl_event_log),
        ])

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
        gate_report = self._gate_runner.run(snapshot)
        for result in gate_report.results:
            render_gate_result(result)

        if not gate_report.all_passed:
            blocker = next(r for r in gate_report.results if not r.passed)
            render_gate_blocked(blocker)
            event = SignalEvent(
                event_time=datetime.now(tz=timezone.utc),
                symbol=symbol,
                snapshot=snapshot.model_dump(mode="json"),
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="BLOCKED",
                final_reason=blocker.reason,
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
            pre_gate_report=gate_report.model_dump(mode="json"),
            final_outcome="SNAPSHOT_ONLY",
            final_reason="All gates passed. LLM layer not yet wired.",
        )
        self.event_logger.append_event(event)
        render_success(f"cycle logged to {self.config.storage.jsonl_event_log}")

    def run_forever(self) -> None:
        while True:
            self.run_once()
            sleep(self.config.app.loop_interval_seconds)


