from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import sleep

log = logging.getLogger(__name__)

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
from drift.storage.outcome_resolver import resolve_live_outcomes
from drift.storage.trade_store import TradeStore
from drift.storage.signal_store import SignalStore
from drift.storage.watch_store import WatchStore


class DriftApplication:
    def __init__(self, config: AppConfig, config_path: str, sandbox: bool = False, manual_run: bool = False, watch_trigger: bool = False, trigger: str = "scheduled") -> None:
        self.config = config
        self.config_path = config_path
        self._sandbox = sandbox
        self._source = "sandbox" if sandbox else "live"
        self._trigger = trigger

        # Sandbox mode writes to isolated file paths so it cannot contaminate
        # production signal data.
        jsonl_path = config.storage.sandbox_jsonl_event_log if sandbox else config.storage.jsonl_event_log
        sqlite_path = config.storage.sandbox_sqlite_path if sandbox else (
            config.storage.sqlite_path if config.storage.use_sqlite else None
        )
        self.event_logger = EventLogger(jsonl_path, sqlite_path)
        self._signal_store: SignalStore | None = (
            SignalStore(sqlite_path) if sqlite_path else None
        )
        self._watch_store: WatchStore | None = (
            WatchStore(sqlite_path) if sqlite_path else None
        )
        self._trade_store: TradeStore | None = (
            TradeStore(sqlite_path) if (sqlite_path and config.broker.enabled) else None
        )
        self._provider = YFinanceProvider()
        self._engine = FeatureEngine(config)

        # In sandbox mode disable the session gate so signals flow through
        # regardless of time of day, and disable the cooldown gate so repeated
        # test runs aren't blocked by prior signal history.
        # Manual runs (Run Now) and watch-triggered cycles also skip the cooldown
        # gate: the operator / watch condition is the explicit trigger, so the
        # cooldown timer is not meaningful.
        sessions_cfg = config.sessions.model_copy(update={"enabled": False}) if sandbox else config.sessions
        cooldown_cfg = (
            config.gates.model_copy(update={"cooldown_enabled": False})
            if (sandbox or manual_run or watch_trigger)
            else config.gates
        )

        self._gate_runner = GateRunner([
            KillSwitchGate(config.gates),
            SessionGate(sessions_cfg),
            CalendarGate(config.calendar),
            NewsGate(config.gates),
            RegimeGate(config.gates),
            CooldownGate(cooldown_cfg, config.risk, jsonl_path, db_path=sqlite_path),
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
        # Auto-resolve pending live outcomes (non-blocking — failures are logged)
        # ------------------------------------------------------------------
        if self._signal_store and not self._sandbox:
            try:
                resolved = resolve_live_outcomes(self._signal_store, symbol, self._provider)
                if resolved:
                    render_status(f"auto-resolved {resolved} pending live signal(s)")
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning("Outcome resolver error: %s", exc)

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
            reference_time=datetime.now(timezone.utc),
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
                trigger=self._trigger,
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
        # Deterministic strategy scanner (bypasses LLM on clean setups)
        # ------------------------------------------------------------------
        if self.config.liquidity_sweep.enabled:
            from drift.strategy import sweep_scanner
            setup = sweep_scanner.scan(bars_5m, self.config)
            if setup.decision in ("LONG", "SHORT"):
                render_status(
                    f"deterministic scanner: {setup.decision} — {setup.setup_type} "
                    f"(conf={setup.confidence}, RR={setup.reward_risk_ratio})"
                )
                plan = self._build_plan_from_setup(setup, snapshot)
                if plan is not None:
                    render_trade_plan(plan)
                    event = SignalEvent(
                        event_time=datetime.now(tz=timezone.utc),
                        symbol=symbol,
                        source=self._source,
                        trigger=self._trigger,
                        snapshot=snapshot.model_dump(mode="json"),
                        llm_decision_raw=None,
                        llm_decision_parsed=None,
                        pre_gate_report=gate_report.model_dump(mode="json"),
                        trade_plan=plan.model_dump(mode="json"),
                        final_outcome="TRADE_PLAN_ISSUED",
                        final_reason=(
                            f"{plan.bias} | {plan.setup_type} | confidence={plan.confidence} "
                            "(deterministic scanner — LLM skipped)"
                        ),
                    )
                    self.event_logger.append_event(event)
                    render_success(
                        f"deterministic trade plan logged to {self.config.storage.jsonl_event_log}"
                    )
                    if self._trade_store is not None and not self._sandbox:
                        signal_key = event.compute_signal_key()
                        self._trade_store.create(
                            signal_key=signal_key,
                            symbol=plan.symbol,
                            bias=plan.bias,
                            setup_type=plan.setup_type,
                            entry_min=plan.entry_min,
                            entry_max=plan.entry_max,
                            stop_loss=plan.stop_loss,
                            take_profit_1=plan.take_profit_1,
                            take_profit_2=plan.take_profit_2,
                            thesis=plan.thesis,
                            confidence=plan.confidence,
                            max_hold_minutes=plan.max_hold_minutes,
                            generated_at=plan.generated_at.isoformat(),
                            source="live",
                        )
                        log.info(
                            "Deterministic trade created for approval (signal_key=%s)", signal_key
                        )
                    if self.config.output.desktop_notifications:
                        notify_signal(plan, approval_required=self.config.broker.enabled)
                    return "TRADE_PLAN_ISSUED"
                else:
                    render_status(
                        "deterministic scanner signal rejected by plan constraints — "
                        "falling through to LLM"
                    )
            else:
                log.debug("Deterministic scanner: NO_TRADE — %s", setup.no_trade_reason)

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
                trigger=self._trigger,
                snapshot=snapshot.model_dump(mode="json"),
                llm_decision_raw={"text": raw_text},
                llm_decision_parsed=raw_dict,
                pre_gate_report=gate_report.model_dump(mode="json"),
                final_outcome="LLM_NO_TRADE",
                final_reason=decision.thesis,
            )
            sig_key = event.ensure_signal_key().signal_key
            self.event_logger.append_event(event)
            # Save watch conditions so the fast-poll watcher can monitor them.
            if self._watch_store and not self._sandbox and decision.watch_conditions:
                self._watch_store.replace_watches(
                    symbol, decision.watch_conditions, source_signal_key=sig_key
                )
                render_status(
                    f"{len(decision.watch_conditions)} watch condition(s) set — "
                    "monitoring in real-time"
                )
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
                trigger=self._trigger,
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
            trigger=self._trigger,
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

        # ------------------------------------------------------------------
        # Broker integration — create a pending order awaiting approval
        # ------------------------------------------------------------------
        if self._trade_store is not None and not self._sandbox:
            signal_key = event.compute_signal_key()
            self._trade_store.create(
                signal_key=signal_key,
                symbol=plan.symbol,
                bias=plan.bias,
                setup_type=plan.setup_type,
                entry_min=plan.entry_min,
                entry_max=plan.entry_max,
                stop_loss=plan.stop_loss,
                take_profit_1=plan.take_profit_1,
                take_profit_2=plan.take_profit_2,
                thesis=plan.thesis,
                confidence=plan.confidence,
                max_hold_minutes=plan.max_hold_minutes,
                generated_at=plan.generated_at.isoformat(),
                source="live",
            )
            log.info("Trade created for approval (signal_key=%s)", signal_key)

        if self.config.output.desktop_notifications:
            notify_signal(plan, approval_required=self.config.broker.enabled)

        return "TRADE_PLAN_ISSUED"

    def _build_plan_from_setup(self, setup: object, snapshot: MarketSnapshot) -> object | None:
        """Map a deterministic SetupResult to a TradePlan.

        Returns None if the plan fails the risk constraints defined in config.risk
        (e.g. min_confidence, min_reward_risk).  This mirrors the guard in
        TradePlanBuilder.build() so the same operator-facing constraints apply to
        both paths.
        """
        from drift.models import TradePlan

        cfg = self.config.risk
        if setup.confidence < cfg.min_confidence:
            log.debug(
                "Deterministic plan rejected: confidence %d < min %d",
                setup.confidence, cfg.min_confidence,
            )
            return None

        if setup.reward_risk_ratio is None or setup.reward_risk_ratio < cfg.min_reward_risk:
            log.debug(
                "Deterministic plan rejected: R:R %.2f < min %.2f",
                setup.reward_risk_ratio or 0, cfg.min_reward_risk,
            )
            return None

        return TradePlan(
            symbol=self.config.instrument.symbol,
            bias=setup.decision,
            setup_type=setup.setup_type,
            confidence=setup.confidence,
            entry_min=setup.entry_min,
            entry_max=setup.entry_max,
            stop_loss=setup.stop_loss,
            take_profit_1=setup.take_profit_1,
            take_profit_2=setup.take_profit_2,
            reward_risk_ratio=setup.reward_risk_ratio,
            max_hold_minutes=cfg.max_hold_minutes_default,
            thesis=setup.thesis,
            invalidation_conditions=setup.invalidation_conditions,
            do_not_trade_if=setup.invalidation_conditions,
            operator_instructions=[
                f"Entry zone {setup.entry_min:.2f}–{setup.entry_max:.2f}.",
                f"Stop {setup.stop_loss:.2f}.",
                f"TP1 {setup.take_profit_1:.2f}"
                + (f" / TP2 {setup.take_profit_2:.2f}" if setup.take_profit_2 else ""),
                f"Confirmation: {setup.confirmation_type.replace('_', ' ')}.",
            ],
        )

    def run_forever(self) -> None:
        while True:
            self.run_once()
            sleep(self.config.app.loop_interval_seconds)

    def run_sweep_only(self) -> str:
        """Run a lightweight cycle: fetch data, evaluate gates, run sweep scanner only.

        The LLM is never called.  Used by the dedicated deterministic scan loop
        which fires every ``liquidity_sweep.scan_interval_seconds`` (default 5 min)
        so time-sensitive setups are caught within one bar of the confirmation
        candle closing — not up to 15 minutes later.

        Returns one of: ``"BLOCKED"``, ``"DET_NO_TRADE"``, ``"TRADE_PLAN_ISSUED"``,
        ``"DET_DISABLED"``, ``"NO_DATA"``.
        """
        if not self.config.liquidity_sweep.enabled:
            return "DET_DISABLED"

        symbol = self.config.instrument.symbol

        # Fetch data
        try:
            last_price = self._provider.get_latest_quote(symbol)
        except ValueError as exc:
            log.debug("run_sweep_only: data error — %s", exc)
            return "NO_DATA"

        session = self._provider.get_session_status(symbol)
        bars_1m = self._provider.get_recent_bars(symbol, "1m", self.config.lookbacks.bars_1m)
        bars_5m = self._provider.get_recent_bars(symbol, "5m", self.config.lookbacks.bars_5m)
        bars_1h = self._provider.get_recent_bars(symbol, "1h", self.config.lookbacks.bars_1h)

        # Compute features → snapshot
        snapshot: MarketSnapshot = self._engine.compute(
            bars_1m=bars_1m,
            bars_5m=bars_5m,
            bars_1h=bars_1h,
            last_price=last_price,
            session=session,
            reference_time=datetime.now(timezone.utc),
        )

        # Gate layer (same gates as run_once — respect session, cooldown, kill-switch, etc.)
        gate_report = self._gate_runner.run(snapshot)
        if not gate_report.all_passed:
            blocker = next(r for r in gate_report.results if not r.passed)
            log.debug("run_sweep_only: blocked by %s — %s", blocker.gate_name, blocker.reason)
            return "BLOCKED"

        # Sweep scanner
        from drift.strategy import sweep_scanner
        setup = sweep_scanner.scan(bars_5m, self.config)
        if setup.decision not in ("LONG", "SHORT"):
            log.debug("run_sweep_only: NO_TRADE — %s", setup.no_trade_reason)
            return "DET_NO_TRADE"

        log.info(
            "run_sweep_only: %s %s (conf=%d, RR=%s)",
            setup.decision, setup.setup_type, setup.confidence, setup.reward_risk_ratio,
        )
        plan = self._build_plan_from_setup(setup, snapshot)
        if plan is None:
            log.debug("run_sweep_only: plan rejected by constraints")
            return "DET_NO_TRADE"

        render_trade_plan(plan)
        event = SignalEvent(
            event_time=datetime.now(tz=timezone.utc),
            symbol=symbol,
            source=self._source,
            trigger=self._trigger,
            snapshot=snapshot.model_dump(mode="json"),
            llm_decision_raw=None,
            llm_decision_parsed=None,
            pre_gate_report=gate_report.model_dump(mode="json"),
            trade_plan=plan.model_dump(mode="json"),
            final_outcome="TRADE_PLAN_ISSUED",
            final_reason=(
                f"{plan.bias} | {plan.setup_type} | confidence={plan.confidence} "
                "(deterministic scanner — dedicated 5m cycle)"
            ),
        )
        self.event_logger.append_event(event)
        if self._trade_store is not None and not self._sandbox:
            signal_key = event.compute_signal_key()
            self._trade_store.create(
                signal_key=signal_key,
                symbol=plan.symbol,
                bias=plan.bias,
                setup_type=plan.setup_type,
                entry_min=plan.entry_min,
                entry_max=plan.entry_max,
                stop_loss=plan.stop_loss,
                take_profit_1=plan.take_profit_1,
                take_profit_2=plan.take_profit_2,
                thesis=plan.thesis,
                confidence=plan.confidence,
                max_hold_minutes=plan.max_hold_minutes,
                generated_at=plan.generated_at.isoformat(),
                source="live",
            )
        if self.config.output.desktop_notifications:
            notify_signal(plan, approval_required=self.config.broker.enabled)
        return "TRADE_PLAN_ISSUED"


