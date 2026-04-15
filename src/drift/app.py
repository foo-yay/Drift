from __future__ import annotations

from datetime import datetime, timezone
from time import sleep

from drift.ai.client import LLMClient
from drift.ai.mock_client import MockLLMClient
from drift.config.models import AppConfig
from drift.data.providers.yfinance_provider import YFinanceProvider
from drift.features.engine import FeatureEngine
from drift.gates.calendar_gate import CalendarGate
from drift.gates.cooldown_gate import CooldownGate
from drift.gates.kill_switch_gate import KillSwitchGate
from drift.gates.news_gate import NewsGate
from drift.gates.regime_gate import RegimeGate
from drift.gates.runner import GateRunner
from drift.gates.session_gate import SessionGate
from drift.models import MarketSnapshot, SignalEvent
from drift.output.console import (
    render_gate_blocked,
    render_gate_result,
    render_llm_decision,
    render_no_trade,
    render_snapshot,
    render_startup,
    render_status,
    render_success,
    render_trade_plan,
)
from drift.output.notifications import notify_signal
from drift.planning.trade_plan_builder import TradePlanBuilder
from drift.storage.logger import EventLogger


class DriftApplication:
    def __init__(self, config: AppConfig, config_path: str, sandbox: bool = False, manual_run: bool = False) -> None:
        self.config = config
        self.config_path = config_path
        self._sandbox = sandbox
        self._source = "sandbox" if sandbox else "live"

        # Sandbox mode writes to isolated file paths so it cannot contaminate
        # production signal data.
        jsonl_path = config.storage.sandbox_jsonl_event_log if sandbox else config.storage.jsonl_event_log
        sqlite_path = config.storage.sandbox_sqlite_path if sandbox else (
            config.storage.sqlite_path if config.storage.use_sqlite else None
        )
        self.event_logger = EventLogger(jsonl_path, sqlite_path)
        self._provider = YFinanceProvider()
        self._engine = FeatureEngine(config)

        # In sandbox mode disable the session gate so signals flow through
        # regardless of time of day, and disable the cooldown gate so repeated
        # test runs aren't blocked by prior signal history.
        # Manual runs (Run Now) also skip the cooldown gate — the operator
        # is deliberately triggering the cycle so cooldown is not meaningful.
        sessions_cfg = config.sessions.model_copy(update={"enabled": False}) if sandbox else config.sessions
        cooldown_cfg = (
            config.gates.model_copy(update={"cooldown_enabled": False})
            if (sandbox or manual_run)
            else config.gates
        )

        self._gate_runner = GateRunner([
            KillSwitchGate(config.gates),
            SessionGate(sessions_cfg),
            CalendarGate(config.calendar),
            NewsGate(config.gates),
            RegimeGate(config.gates),
            CooldownGate(cooldown_cfg, config.risk, jsonl_path),
        ])
        self._llm_client = MockLLMClient() if sandbox else LLMClient(
            config.llm, log_path=jsonl_path
        )
        self._plan_builder = TradePlanBuilder(config)

    def run_once(self) -> str:
        """Run one analysis cycle and return the final_outcome string.

        Returns one of: ``"BLOCKED"``, ``"LLM_NO_TRADE"``, ``"TRADE_PLAN_ISSUED"``,
        ``"NO_DATA"`` (data fetch failed).
        """
        render_startup(self.config, self.config_path, sandbox=self._sandbox)

        symbol = self.config.instrument.symbol

        # ------------------------------------------------------------------
        # Fetch market data
        # ------------------------------------------------------------------
        render_status("fetching market data...")
        try:
            last_price = self._provider.get_latest_quote(symbol)
        except ValueError as exc:
            render_status(f"[red]data error:[/red] {exc} — skipping cycle")
            return "NO_DATA"

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
                source=self._source,
                snapshot=snapshot.model_dump(mode="json"),
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="BLOCKED",
                final_reason=blocker.reason,
            )
            self.event_logger.append_event(event)
            render_success(f"blocked cycle logged to {self.config.storage.jsonl_event_log}")
            return "BLOCKED"

        # ------------------------------------------------------------------
        # Log the cycle event (all gates passed)
        # ------------------------------------------------------------------

        # ------------------------------------------------------------------
        # LLM adjudication
        # ------------------------------------------------------------------
        render_status("calling LLM...")
        decision, raw_dict, raw_text = self._llm_client.adjudicate(snapshot, gate_report)
        render_llm_decision(decision)

        if decision.decision == "NO_TRADE":
            event = SignalEvent(
                event_time=datetime.now(tz=timezone.utc),
                symbol=symbol,
                source=self._source,
                snapshot=snapshot.model_dump(mode="json"),
                llm_decision_raw={"text": raw_text},
                llm_decision_parsed=raw_dict,
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="LLM_NO_TRADE",
                final_reason=decision.thesis,
            )
            self.event_logger.append_event(event)
            render_no_trade(decision, "LLM returned NO_TRADE.")
            render_success(f"no-trade cycle logged to {self.config.storage.jsonl_event_log}")
            return "LLM_NO_TRADE"

        # ------------------------------------------------------------------
        # Trade plan construction (post-LLM deterministic gates)
        # ------------------------------------------------------------------
        render_status("building trade plan...")
        plan = self._plan_builder.build(snapshot, decision)

        if plan is None:
            event = SignalEvent(
                event_time=datetime.now(tz=timezone.utc),
                symbol=symbol,
                source=self._source,
                snapshot=snapshot.model_dump(mode="json"),
                llm_decision_raw={"text": raw_text},
                llm_decision_parsed=raw_dict,
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="LLM_NO_TRADE",
                final_reason="Trade plan builder rejected signal (stop/R:R/confidence constraint).",
            )
            self.event_logger.append_event(event)
            render_no_trade(decision, "Signal rejected by trade plan constraints (stop/R:R/confidence).")
            render_success(f"rejected cycle logged to {self.config.storage.jsonl_event_log}")
            return "LLM_NO_TRADE"

        # ------------------------------------------------------------------
        # Emit the trade plan
        # ------------------------------------------------------------------
        render_trade_plan(plan)

        event = SignalEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol=symbol,
            source=self._source,
            snapshot=snapshot.model_dump(mode="json"),
            llm_decision_raw={"text": raw_text},
            llm_decision_parsed=raw_dict,
            pre_gate_report=gate_report.model_dump(mode="json"),
            trade_plan=plan.model_dump(mode="json"),
            final_outcome="TRADE_PLAN_ISSUED",
            final_reason=f"{plan.bias} | {plan.setup_type} | confidence={plan.confidence}",
        )
        self.event_logger.append_event(event)
        render_success(f"trade plan logged to {self.config.storage.jsonl_event_log}")

        if self.config.output.desktop_notifications:
            notify_signal(plan)

        return "TRADE_PLAN_ISSUED"

    def run_forever(self) -> None:
        while True:
            self.run_once()
            sleep(self.config.app.loop_interval_seconds)


