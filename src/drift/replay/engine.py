"""ReplayEngine — drives the full Drift pipeline over historical bars.

The engine steps through 1m bars one at a time, running exactly the same
decision path as the live loop:

    ReplayProvider → FeatureEngine → GateRunner → LLMClient
        → TradePlanBuilder → EventLogger → console render

This means every signal produced during replay is auditable under the same
rules as live signals.  The only differences from live mode are:

- Data comes from ``ReplayProvider`` instead of ``YFinanceProvider``.
- An explicit step interval controls how frequently the pipeline fires
  (default: every 15 bars = every 15 minutes of 1m data, matching the live
  900s cadence).
- The session gate is left enabled so you can see how it would have blocked
  out-of-session bars naturally (pass ``disable_session_gate=True`` to skip).
- ``MockLLMClient`` is used by default to avoid API costs.  Pass a real
  ``LLMClient`` instance to adjudicate with Claude.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from drift.config.models import AppConfig
from drift.features.engine import FeatureEngine
from drift.gates.calendar_gate import CalendarGate
from drift.gates.cooldown_gate import CooldownGate
from drift.gates.kill_switch_gate import KillSwitchGate
from drift.gates.regime_gate import RegimeGate
from drift.gates.runner import GateRunner
from drift.gates.session_gate import SessionGate
from drift.models import Bar, LLMDecision, MarketSnapshot, SignalEvent, TradePlan, GateReport
from drift.output.console import (
    render_gate_blocked,
    render_gate_result,
    render_llm_decision,
    render_no_trade,
    render_snapshot,
    render_status,
    render_success,
    render_trade_plan,
)
from drift.planning.trade_plan_builder import TradePlanBuilder
from drift.replay.provider import ReplayProvider
from drift.storage.logger import EventLogger


class LLMClientProtocol(Protocol):
    def adjudicate(
        self,
        snapshot: MarketSnapshot,
        gate_report: GateReport,
    ) -> tuple[LLMDecision, dict, str]: ...


@dataclass
class ReplaySummary:
    """Aggregate statistics produced after a replay run."""

    total_steps: int = 0
    pipeline_steps: int = 0  # steps where the pipeline actually fired
    blocked: int = 0
    llm_no_trade: int = 0
    trade_plans_issued: int = 0
    events: list[SignalEvent] = field(default_factory=list)

    @property
    def signal_rate_pct(self) -> float:
        if self.pipeline_steps == 0:
            return 0.0
        return round(self.trade_plans_issued / self.pipeline_steps * 100, 1)


class ReplayEngine:
    """Steps through historical bars and runs the full Drift pipeline.

    Args:
        config:               App configuration (same object used in live mode).
        bars_1m:              All 1m bars for the replay period, oldest-first.
        bars_5m:              All 5m bars for the replay period, oldest-first.
        bars_1h:              All 1h bars for the replay period, oldest-first.
        llm_client:           LLM client to use.  Defaults to ``MockLLMClient``.
        step_every_n_bars:    Fire the pipeline every N 1m bars (default 15 →
                              every 15 minutes, matching the 900s live cadence).
        disable_session_gate: If True, session gate is disabled so bars outside
                              RTH still pass through.
        verbose:              If True, render the full snapshot panel on each step.
    """

    def __init__(
        self,
        config: AppConfig,
        bars_1m: list[Bar],
        bars_5m: list[Bar],
        bars_1h: list[Bar],
        llm_client: LLMClientProtocol | None = None,
        step_every_n_bars: int = 15,
        disable_session_gate: bool = False,
        verbose: bool = False,
    ) -> None:
        if llm_client is None:
            from drift.ai.mock_client import MockLLMClient
            llm_client = MockLLMClient()

        self._config = config
        self._provider = ReplayProvider(bars_1m, bars_5m, bars_1h, config.instrument.symbol)
        self._engine = FeatureEngine(config)
        self._llm_client = llm_client
        self._plan_builder = TradePlanBuilder(config)
        self._event_logger = EventLogger(config.storage.jsonl_event_log)
        self._step_every_n = max(1, step_every_n_bars)
        self._verbose = verbose

        sessions_cfg = (
            config.sessions.model_copy(update={"enabled": False})
            if disable_session_gate
            else config.sessions
        )
        self._gate_runner = GateRunner([
            KillSwitchGate(config.gates),
            SessionGate(sessions_cfg),
            CalendarGate(config.calendar),
            RegimeGate(config.gates),
            CooldownGate(config.gates, config.risk, config.storage.jsonl_event_log),
        ])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> ReplaySummary:
        """Run the replay from start to end and return a summary.

        The provider cursor starts at index 0.  We always process the first
        bar, then advance and process every ``step_every_n_bars`` bars.
        """
        summary = ReplaySummary()
        step = 0

        while True:
            summary.total_steps += 1

            if step % self._step_every_n == 0:
                event = self._run_pipeline_step()
                summary.pipeline_steps += 1
                if event is not None:
                    summary.events.append(event)
                    if event.final_outcome == "BLOCKED":
                        summary.blocked += 1
                    elif event.final_outcome == "LLM_NO_TRADE":
                        summary.llm_no_trade += 1
                    elif event.final_outcome == "TRADE_PLAN_ISSUED":
                        summary.trade_plans_issued += 1

            if not self._provider.has_next():
                break

            self._provider.advance()
            step += 1

        return summary

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_pipeline_step(self) -> SignalEvent | None:
        """Run one full pipeline cycle at the current provider cursor.

        Returns a ``SignalEvent`` for every outcome (BLOCKED, LLM_NO_TRADE,
        or TRADE_PLAN_ISSUED), or None only if data fetch failed.
        """
        symbol = self._config.instrument.symbol
        ts = self._provider.current_timestamp

        if self._verbose:
            render_status(
                f"[replay] {ts.strftime('%Y-%m-%d %H:%M')} UTC  "
                f"(step {self._provider.cursor + 1}/{self._provider.total_steps})"
            )

        # Fetch data via the replay provider (no network call)
        try:
            last_price = self._provider.get_latest_quote(symbol)
        except (IndexError, ValueError) as exc:
            render_status(f"[red]data error:[/red] {exc} — skipping step")
            return None

        session = self._provider.get_session_status(symbol)
        bars_1m = self._provider.get_recent_bars(symbol, "1m", self._config.lookbacks.bars_1m)
        bars_5m = self._provider.get_recent_bars(symbol, "5m", self._config.lookbacks.bars_5m)
        bars_1h = self._provider.get_recent_bars(symbol, "1h", self._config.lookbacks.bars_1h)

        # Feature engine
        snapshot: MarketSnapshot = self._engine.compute(
            bars_1m=bars_1m,
            bars_5m=bars_5m,
            bars_1h=bars_1h,
            last_price=last_price,
            session=session,
        )
        if self._verbose:
            render_snapshot(snapshot)

        # Gate layer
        gate_report = self._gate_runner.run(snapshot)
        if self._verbose:
            for result in gate_report.results:
                render_gate_result(result)

        if not gate_report.all_passed:
            blocker = next(r for r in gate_report.results if not r.passed)
            if self._verbose:
                render_gate_blocked(blocker)
            else:
                from drift.output.console import console
                console.print(
                    f"[dim][replay] {ts.strftime('%Y-%m-%d %H:%M')} — "
                    f"BLOCKED: {blocker.reason}[/dim]"
                )
            event = SignalEvent(
                event_time=ts,
                symbol=symbol,
                snapshot=snapshot.model_dump(mode="json"),
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="BLOCKED",
                final_reason=blocker.reason,
            )
            self._event_logger.append_event(event)
            return event

        # LLM adjudication
        if self._verbose:
            render_status(
                f"[replay] {ts.strftime('%Y-%m-%d %H:%M')} UTC — gates passed, calling LLM..."
            )
        decision, raw_dict, raw_text = self._llm_client.adjudicate(snapshot, gate_report)
        if self._verbose:
            render_llm_decision(decision)

        if decision.decision == "NO_TRADE":
            event = SignalEvent(
                event_time=ts,
                symbol=symbol,
                snapshot=snapshot.model_dump(mode="json"),
                llm_decision_raw={"text": raw_text},
                llm_decision_parsed=raw_dict,
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="LLM_NO_TRADE",
                final_reason=decision.thesis,
            )
            self._event_logger.append_event(event)
            if self._verbose:
                render_no_trade(decision, "LLM returned NO_TRADE.")
            else:
                from drift.output.console import console
                console.print(
                    f"[dim][replay] {ts.strftime('%Y-%m-%d %H:%M')} — LLM NO_TRADE[/dim]"
                )
            return event

        # Trade plan construction
        plan = self._plan_builder.build(snapshot, decision)

        if plan is None:
            event = SignalEvent(
                event_time=ts,
                symbol=symbol,
                snapshot=snapshot.model_dump(mode="json"),
                llm_decision_raw={"text": raw_text},
                llm_decision_parsed=raw_dict,
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="LLM_NO_TRADE",
                final_reason="Trade plan builder rejected signal (stop/R:R/confidence constraint).",
            )
            self._event_logger.append_event(event)
            if self._verbose:
                render_no_trade(decision, "Signal rejected by trade plan constraints.")
            else:
                from drift.output.console import console
                console.print(
                    f"[dim][replay] {ts.strftime('%Y-%m-%d %H:%M')} — "
                    f"REJECTED: plan constraints[/dim]"
                )
            return event

        if self._verbose:
            render_trade_plan(plan)
        event = SignalEvent(
            event_time=ts,
            symbol=symbol,
            snapshot=snapshot.model_dump(mode="json"),
            llm_decision_raw={"text": raw_text},
            llm_decision_parsed=raw_dict,
            pre_gate_report=gate_report.model_dump(mode="json"),
            trade_plan=plan.model_dump(mode="json"),
            final_outcome="TRADE_PLAN_ISSUED",
            final_reason=f"{plan.bias} | {plan.setup_type} | confidence={plan.confidence}",
        )
        self._event_logger.append_event(event)
        if self._verbose:
            render_success(f"trade plan issued at {ts.strftime('%H:%M')} UTC")
        else:
            from drift.output.console import console
            console.print(
                f"[green][replay] {ts.strftime('%Y-%m-%d %H:%M')} — "
                f"SIGNAL: {plan.bias} | {plan.setup_type} | conf={plan.confidence}[/green]"
            )
        return event
