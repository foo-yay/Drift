"""Background analysis scheduler for the Drift GUI.

Runs ``DriftApplication.run_once()`` on the configured loop interval in a
daemon thread that is independent of any browser connection.  The Streamlit
process is the lifetime boundary — as long as ``drift gui`` is running in
the terminal, cycles execute on schedule whether or not a browser tab is open.

A second daemon thread polls active watch conditions every 30 seconds.  When a
condition is met, it fires an unscheduled ``run_once()`` immediately and clears
the watch, so opportunities spotted by the LLM are never missed due to the
15-minute polling interval.

Usage (called once from app.py at startup)::

    from drift.gui.scheduler import ensure_scheduler_running
    ensure_scheduler_running()

The scheduler is a ``@st.cache_resource`` singleton — it is created exactly
once per Streamlit server process and lives until the process exits.
"""
from __future__ import annotations

import io
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

log = logging.getLogger(__name__)

_WATCH_POLL_INTERVAL = 30  # seconds between watch condition checks
_WATCH_GRACE_SECONDS = 300          # minimum gap between back-to-back NO_TRADE watch cycles (5 min)
_WATCH_TRADE_PLAN_GRACE_SECONDS = 900  # minimum gap after a watch-triggered TRADE_PLAN_ISSUED (15 min)


class _SchedulerState:
    """Mutable state bag shared between the daemon thread and the GUI."""

    def __init__(self) -> None:
        self.last_run_utc: datetime | None = None
        self.last_scheduled_run_utc: datetime | None = None
        self.next_run_utc: datetime | None = None
        self.last_outcome: str = ""          # "success" | "error" | ""
        self.last_error: str = ""
        self.running: bool = False
        self.cycle_count: int = 0
        self.watch_triggered_count: int = 0  # total watch-triggered cycles fired
        self._lock = threading.Lock()

    # Thread-safe snapshot for the GUI to read.
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "last_run_utc":          self.last_run_utc,
                "last_scheduled_run_utc": self.last_scheduled_run_utc,
                "next_run_utc":          self.next_run_utc,
                "last_outcome":          self.last_outcome,
                "last_error":            self.last_error,
                "running":               self.running,
                "cycle_count":           self.cycle_count,
                "watch_triggered_count": self.watch_triggered_count,
            }

    def record_run(self, outcome: str, error: str = "") -> None:
        with self._lock:
            self.last_run_utc = datetime.now(tz=timezone.utc)
            self.last_outcome = outcome
            self.last_error   = error
            self.running      = False
            self.cycle_count += 1

    def record_watch_trigger(self) -> None:
        with self._lock:
            self.watch_triggered_count += 1

    def mark_running(self, next_run_utc: datetime | None = None) -> None:
        with self._lock:
            self.running = True
            if next_run_utc is not None:
                self.next_run_utc = next_run_utc


class BackgroundScheduler:
    """Daemon thread that runs analysis cycles on the configured interval.

    Also spawns a second daemon thread that polls active watch conditions
    every 30 seconds and fires an immediate cycle when any condition is met.

    When a scheduled cycle is blocked by the cooldown gate the scheduler
    calculates exactly how many seconds remain in the cooldown window and
    arms a one-shot ``threading.Timer``.  That timer fires a single extra
    cycle the moment the window expires, so no opportunity is missed due to
    the fixed loop interval being out of phase with the cooldown expiry.
    The cooldown duration itself is driven by ``max_hold_minutes`` stored on
    the most-recent trade-plan event in the JSONL log, falling back to the
    configured ``risk.cooldown_minutes`` when that field is absent.
    """

    def __init__(self, config_path: str, loop_interval_seconds: int) -> None:
        self._config_path = config_path
        self._interval = loop_interval_seconds
        self.state = _SchedulerState()
        # One-shot timer that fires a cycle exactly when the cooldown expires.
        self._cooldown_timer: threading.Timer | None = None
        self._cooldown_timer_lock = threading.Lock()
        # Track plan IDs we've already acted on (TP/SL hit) so the watch loop
        # doesn't fire a second cycle for the same resolved plan.
        self._resolved_plan_ids: set[int] = set()
        # Track whether price has ever touched the entry zone for each plan (by DB id).
        # Populated continuously by the watch loop using 1m bar OHLC overlap.
        self._entry_zone_touched: dict[int, bool] = {}
        # Track WORKING orders that have already been auto-assessed after fill timeout.
        self._fill_timeout_assessed: set[str] = set()
        # Prevent watch-triggered cycles from cascading: track when the last
        # watch-triggered cycle fired, whether it produced a trade plan, and
        # enforce an appropriate minimum grace period before the next fires.
        self._last_watch_cycle_utc: datetime | None = None
        self._last_watch_was_trade_plan: bool = False
        # Post-close cycle deferral: if a cycle is running when a position
        # closes, the close events are queued here and drained the moment the
        # running cycle finishes rather than being silently dropped.
        self._pending_post_close_events: list[dict] = []
        self._pending_post_close_lock = threading.Lock()
        # Serialise main and det cycles so they never overlap (both write to
        # the same DB and log files).
        self._cycle_lock = threading.Lock()
        # UTC timestamp of the last completed main cycle — the det loop skips
        # firing if a main cycle ran recently (it already ran the scanner).
        self._last_main_run_utc: datetime | None = None
        self._thread = threading.Thread(
            target=self._loop,
            name="drift-scheduler",
            daemon=True,         # dies automatically when the process exits
        )
        self._stop_event = threading.Event()  # set to request graceful shutdown
        self._thread.start()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            name="drift-watch-poller",
            daemon=True,
        )
        self._watch_thread.start()
        self._expiry_thread = threading.Thread(
            target=self._position_expiry_loop,
            name="drift-position-monitor",
            daemon=True,
        )
        self._expiry_thread.start()
        # Dedicated deterministic scanner thread — runs at scan_interval_seconds
        # (default 5 min) between full LLM cycles to catch time-sensitive setups.
        self._det_thread = threading.Thread(
            target=self._det_loop,
            name="drift-det-scanner",
            daemon=True,
        )
        self._det_thread.start()
        log.info(
            "BackgroundScheduler started — interval=%ds, watch_poll=%ds",
            loop_interval_seconds, _WATCH_POLL_INTERVAL,
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        """Signal all daemon threads to exit at their next sleep checkpoint."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal loop (daemon thread)
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Run first cycle after a short startup delay, then loop on interval."""
        # Short delay so the process finishes starting up before the first cycle.
        if self._stop_event.wait(timeout=5):
            return
        while not self._stop_event.is_set():
            outcome = self._run_cycle(trigger="scheduled")
            self.state.last_scheduled_run_utc = datetime.now(tz=timezone.utc)
            if outcome == "BLOCKED":
                self._schedule_cooldown_wakeup()
            else:
                self._cancel_cooldown_timer()
            next_run = datetime.now(tz=timezone.utc) + timedelta(seconds=self._interval)
            self.state.next_run_utc = next_run
            self._stop_event.wait(timeout=self._interval)

    def _run_cycle(self, trigger: str = "scheduled") -> str:
        from drift.app import DriftApplication
        from drift.gui.state import _PROJECT_ROOT
        from drift.output import console as console_mod
        from drift.utils.config import load_app_config
        from rich.console import Console

        watch_trigger = trigger == "watch"
        self.state.mark_running()
        buf = io.StringIO()
        capture = Console(file=buf, force_terminal=False, no_color=True, width=100)
        orig = console_mod.console
        console_mod.console = capture
        try:
            config = load_app_config(self._config_path)
            root = _PROJECT_ROOT
            abs_storage = config.storage.model_copy(update={
                "jsonl_event_log":         str(root / config.storage.jsonl_event_log),
                "sqlite_path":             str(root / config.storage.sqlite_path),
                "sandbox_jsonl_event_log": str(root / config.storage.sandbox_jsonl_event_log),
                "sandbox_sqlite_path":     str(root / config.storage.sandbox_sqlite_path),
            })
            abs_config = config.model_copy(update={"storage": abs_storage})
            with self._cycle_lock:
                app = DriftApplication(abs_config, config_path=self._config_path, watch_trigger=watch_trigger, trigger=trigger)
                outcome = app.run_once() or "unknown"
                self._last_main_run_utc = datetime.now(tz=timezone.utc)
            self.state.record_run(outcome)
            self._drain_pending_post_close()
            return outcome
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler cycle error: %s", exc)
            self.state.record_run("error", str(exc))
            self._drain_pending_post_close()
            return "error"
        finally:
            console_mod.console = orig

    # ------------------------------------------------------------------
    # Deterministic scanner loop (separate 5-minute cadence)
    # ------------------------------------------------------------------

    def _det_loop(self) -> None:
        """Run the sweep scanner on its own cadence between full LLM cycles.

        Loads scan_interval_seconds from config on every iteration so changes
        to settings.yaml take effect without restarting the GUI.
        Skips if a main cycle completed within the last scan_interval seconds
        (the main cycle already ran the scanner — no need to double-fire).
        """
        # Stagger startup so this thread doesn't compete with the main loop's
        # first cycle (which fires 5 s after process start).
        if self._stop_event.wait(timeout=15):
            return
        while not self._stop_event.is_set():
            try:
                from drift.utils.config import load_app_config
                config = load_app_config(self._config_path)
                det_interval = config.liquidity_sweep.scan_interval_seconds
            except Exception:  # noqa: BLE001
                det_interval = 300

            # Skip if a main cycle ran recently — it already ran the scanner.
            if self._last_main_run_utc is not None:
                elapsed = (datetime.now(tz=timezone.utc) - self._last_main_run_utc).total_seconds()
                if elapsed < det_interval:
                    self._stop_event.wait(timeout=det_interval - elapsed)
                    continue

            self._run_det_cycle()
            self._stop_event.wait(timeout=det_interval)

    def _run_det_cycle(self) -> str:
        """Run a sweep-scanner-only cycle (no LLM call)."""
        from drift.app import DriftApplication
        from drift.gui.state import _PROJECT_ROOT
        from drift.output import console as console_mod
        from drift.utils.config import load_app_config
        from rich.console import Console

        buf = io.StringIO()
        capture = Console(file=buf, force_terminal=False, no_color=True, width=100)
        orig = console_mod.console
        console_mod.console = capture
        try:
            config = load_app_config(self._config_path)
            root = _PROJECT_ROOT
            abs_storage = config.storage.model_copy(update={
                "jsonl_event_log":         str(root / config.storage.jsonl_event_log),
                "sqlite_path":             str(root / config.storage.sqlite_path),
                "sandbox_jsonl_event_log": str(root / config.storage.sandbox_jsonl_event_log),
                "sandbox_sqlite_path":     str(root / config.storage.sandbox_sqlite_path),
            })
            abs_config = config.model_copy(update={"storage": abs_storage})
            with self._cycle_lock:
                app = DriftApplication(abs_config, config_path=self._config_path, trigger="scheduled")
                outcome = app.run_sweep_only()
            log.debug("Det scan cycle outcome: %s", outcome)
            if outcome == "TRADE_PLAN_ISSUED":
                self._drain_pending_post_close()
            return outcome
        except Exception as exc:  # noqa: BLE001
            log.warning("Det scan cycle error: %s", exc)
            return "error"
        finally:
            console_mod.console = orig

    # ------------------------------------------------------------------
    # Cooldown wakeup helpers
    # ------------------------------------------------------------------

    def _schedule_cooldown_wakeup(self) -> None:
        """Arm a one-shot timer to fire a cycle exactly when cooldown expires.

        Only one pending timer is allowed at a time; any pre-existing timer is
        cancelled before the new one is set.  A 2-second buffer is added so the
        gate evaluation runs comfortably after the window has actually cleared.
        """
        seconds = self._get_cooldown_remaining_seconds()
        if seconds is None or seconds <= 0:
            return
        delay = seconds + 2.0  # small buffer so the gate definitely passes
        with self._cooldown_timer_lock:
            if self._cooldown_timer is not None:
                self._cooldown_timer.cancel()
            self._cooldown_timer = threading.Timer(delay, self._run_once_after_cooldown)
            self._cooldown_timer.daemon = True
            self._cooldown_timer.start()
        log.info(
            "Cooldown wakeup scheduled in %.0fs (%.1f min)",
            delay, delay / 60,
        )

    def _cancel_cooldown_timer(self) -> None:
        """Cancel any pending cooldown wakeup (successful cycle makes it moot)."""
        with self._cooldown_timer_lock:
            if self._cooldown_timer is not None:
                self._cooldown_timer.cancel()
                self._cooldown_timer = None

    def _run_once_after_cooldown(self) -> None:
        """Callback fired by the one-shot cooldown timer."""
        with self._cooldown_timer_lock:
            self._cooldown_timer = None  # mark as consumed
        # Before running a fresh cycle, write expiry outcomes for plans whose
        # time horizon has elapsed without TP/SL being breached.
        self._resolve_expired_plans()
        if not self.state.running:
            log.info("Cooldown expired — firing one-shot unscheduled cycle")
            self._run_cycle(trigger="cooldown")

    def _resolve_expired_plans(self) -> None:
        """Write expiry outcomes for pending live plans whose time horizon has elapsed.

        Called when the cooldown timer fires, before the next analysis cycle.
        Plans that were already resolved via TP/SL (tracked in ``_resolved_plan_ids``)
        are skipped; only truly unresolved plans get an expiry outcome written.
        """
        from drift.gui.state import _PROJECT_ROOT
        from drift.storage.signal_store import SignalStore
        from drift.utils.config import load_app_config
        try:
            config = load_app_config(self._config_path)
            root = _PROJECT_ROOT
            sqlite_path = str(root / config.storage.sqlite_path)
            symbol = config.instrument.symbol
            store = SignalStore(sqlite_path)
            for plan in store.get_pending_live_signals(symbol):
                if plan.id in self._resolved_plan_ids:
                    continue
                zone_touched = self._entry_zone_touched.get(plan.id, False)
                outcome = "EXPIRED" if zone_touched else "EXPIRED_NO_FILL"
                store.resolve_live_signal(plan.id, outcome, 0.0)
                self._resolved_plan_ids.add(plan.id)
                log.info(
                    "Plan #%d expired without TP/SL — outcome: %s (zone_touched=%s)",
                    plan.id, outcome, zone_touched,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not resolve expired plans: %s", exc)

    def _get_cooldown_remaining_seconds(self) -> float | None:
        """Instantiate the cooldown gate and query how many seconds remain."""
        from drift.gates.cooldown_gate import CooldownGate
        from drift.gui.state import _PROJECT_ROOT
        from drift.utils.config import load_app_config
        try:
            config = load_app_config(self._config_path)
            root = _PROJECT_ROOT
            log_path = str(root / config.storage.jsonl_event_log)
            db_path = str(root / config.storage.sqlite_path) if config.storage.use_sqlite else None
            gate = CooldownGate(config.gates, config.risk, log_path, db_path=db_path)
            return gate.seconds_remaining()
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not query cooldown remaining: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Watch condition poll loop (every 30s)
    # ------------------------------------------------------------------

    def _watch_loop(self) -> None:
        """Poll active watches every 30 seconds; fire a cycle when one triggers."""
        if self._stop_event.wait(timeout=10):  # short startup delay
            return
        while not self._stop_event.is_set():
            try:
                self._check_watches()
            except Exception as exc:  # noqa: BLE001
                log.warning("Watch poller error: %s", exc)
            self._stop_event.wait(timeout=_WATCH_POLL_INTERVAL)

    def _check_watches(self) -> None:
        """Evaluate active watch conditions and active trade plan levels; trigger a cycle if any fires."""
        from drift.gui.state import _PROJECT_ROOT
        from drift.storage.signal_store import SignalStore
        from drift.storage.watch_store import WatchStore
        from drift.utils.config import load_app_config

        config = load_app_config(self._config_path)
        if not config.storage.use_sqlite:
            return

        root = _PROJECT_ROOT
        sqlite_path = str(root / config.storage.sqlite_path)
        symbol = config.instrument.symbol

        watch_store = WatchStore(sqlite_path)
        active = watch_store.get_active(symbol)

        # Also monitor the active trade plan's stop / TP levels.
        signal_store = SignalStore(sqlite_path)
        pending_plans = signal_store.get_pending_live_signals(symbol)

        if not active and not pending_plans:
            return

        # Fetch the latest quote once (cheap) for price-based conditions.
        from drift.data.providers.yfinance_provider import YFinanceProvider
        provider = YFinanceProvider()
        try:
            last_price = provider.get_latest_quote(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("Watch poller: could not fetch quote — %s", exc)
            return

        # Fetch 1m bars only if any RSI conditions are active.
        rsi_needed = any(w.condition_type.startswith("rsi") for w in active)
        current_rsi: float | None = None
        if rsi_needed:
            current_rsi = _compute_rsi(provider.get_recent_bars(symbol, "1m", 20))

        triggered_any = False
        for watch in active:
            met = _condition_met(watch.condition_type, watch.value, last_price, current_rsi)
            if met:
                log.info(
                    "Watch triggered: %s %s %.2f (current price=%.2f, rsi=%s) — "
                    "firing unscheduled cycle",
                    watch.condition_type, watch.value, watch.value, last_price, current_rsi,
                )
                watch_store.mark_triggered(watch.id)
                triggered_any = True

        # Monitor the active trade plan: track entry-zone contact via 1m bars,
        # then classify and resolve when a TP or SL level is breached.
        if pending_plans:
            plan = pending_plans[-1]  # most recent unresolved plan

            # Update entry-zone tracking from 1m bar OHLC (only until confirmed).
            if not self._entry_zone_touched.get(plan.id, False):
                if plan.entry_min is not None and plan.entry_max is not None:
                    try:
                        bars_1m = provider.get_recent_bars(symbol, "1m", 5)
                        if _entry_zone_in_bar_range(plan, bars_1m):
                            self._entry_zone_touched[plan.id] = True
                            log.info(
                                "Entry zone [%.2f, %.2f] confirmed touched for plan #%d",
                                plan.entry_min, plan.entry_max, plan.id,
                            )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("1m bar fetch for entry-zone check failed: %s", exc)

            if plan.id not in self._resolved_plan_ids and _trade_plan_level_hit(plan, last_price):
                zone_touched = self._entry_zone_touched.get(plan.id, False)
                outcome, pnl = _classify_resolved_outcome(plan, last_price, zone_touched)
                log.info(
                    "Active trade plan resolved: %s (price=%.2f, zone_touched=%s) → %s (pnl=%.1f pts)",
                    _trade_plan_hit_label(plan, last_price), last_price, zone_touched, outcome, pnl,
                )
                self._resolved_plan_ids.add(plan.id)
                try:
                    signal_store.resolve_live_signal(plan.id, outcome, pnl)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not write plan resolution to DB: %s", exc)
                self._cancel_cooldown_timer()
                triggered_any = True

        if triggered_any and not self.state.running:
            # Enforce a grace period between watch-triggered cycles.
            # After a TRADE_PLAN_ISSUED the grace lasts the full cooldown
            # window (15 min) to prevent duplicate signals. After a NO_TRADE
            # the shorter 5-min grace guards against cascade re-triggering.
            now = datetime.now(tz=timezone.utc)
            if self._last_watch_cycle_utc is not None:
                grace = (
                    _WATCH_TRADE_PLAN_GRACE_SECONDS
                    if self._last_watch_was_trade_plan
                    else _WATCH_GRACE_SECONDS
                )
                elapsed = (now - self._last_watch_cycle_utc).total_seconds()
                if elapsed < grace:
                    log.debug(
                        "Watch grace period active (%s) — %.0fs remaining, skipping",
                        "trade-plan" if self._last_watch_was_trade_plan else "no-trade",
                        grace - elapsed,
                    )
                    return
            self._last_watch_cycle_utc = now
            self.state.record_watch_trigger()
            outcome = self._run_cycle(trigger="watch")
            self._last_watch_was_trade_plan = (outcome == "TRADE_PLAN_ISSUED")

        # Poll active IB positions for fill/exit detection
        # (also runs in the position-monitor thread; having it here too
        #  keeps latency low when the watch loop fires frequently)
        self._poll_positions(config)

    def _poll_positions(self, config) -> None:
        """Poll IB for position state changes (fills, exits).

        When a position *closes* (SL hit, TP hit, etc.) an immediate LLM cycle
        is fired so the system can evaluate whether a new opportunity exists at
        the current price without waiting for the next scheduled interval.
        """
        if not config.broker.enabled:
            return
        try:
            from drift.brokers.position_manager import PositionManager
            from drift.gui.state import _PROJECT_ROOT

            root = _PROJECT_ROOT
            db_path = str(root / config.storage.sqlite_path)
            mgr = PositionManager(config, db_path)
            changes = mgr.poll_positions()
            for ch in changes:
                log.info("Position update: %s", ch)
            mgr.close()

            # If any change is a *close* (not an entry fill), fire an
            # immediate post-close cycle so the LLM can evaluate new setups.
            _CLOSE_TYPES = {"SL_HIT", "TP1_HIT", "TP2_HIT", "MANUAL_HIT"}
            closed = [c for c in changes if c.get("type") in _CLOSE_TYPES
                      or c.get("type", "").endswith("_HIT")]
            if closed:
                self._queue_post_close_cycle(closed)
        except Exception as exc:  # noqa: BLE001
            log.warning("Position polling error: %s", exc)

    def _queue_post_close_cycle(self, close_events: list[dict]) -> None:
        """Schedule a post-close LLM cycle.

        If a cycle is currently running the events are queued and will be
        drained (and fired) the moment that cycle completes via
        ``_drain_pending_post_close``.  If nothing is running, fires
        immediately.
        """
        with self._pending_post_close_lock:
            if self.state.running:
                self._pending_post_close_events.extend(close_events)
                log.info(
                    "Cycle in progress — deferring post-close cycle (%d event(s))",
                    len(close_events),
                )
                return
        # Not running — fire immediately.
        self._fire_post_close_cycle(close_events)

    def _drain_pending_post_close(self) -> None:
        """Fire any post-close cycles that were deferred while a cycle was running.

        Called by ``_run_cycle`` immediately after ``record_run`` so the
        deferred cycle runs as soon as the blocking cycle finishes.
        """
        with self._pending_post_close_lock:
            events = self._pending_post_close_events[:]
            self._pending_post_close_events.clear()
        if events:
            log.info("Draining %d deferred post-close event(s)", len(events))
            self._fire_post_close_cycle(events)

    def _fire_post_close_cycle(self, close_events: list[dict]) -> None:
        """Fire an immediate LLM cycle after a position closes.

        The trade is already closed in the DB, so the cooldown gate's
        ``_check_active_trade()`` will pass through — no special bypass needed.
        Any watch/cooldown timers are cancelled since the market context has
        changed.
        """
        labels = ", ".join(
            f"#{c.get('position_id')} {c.get('type')}" for c in close_events
        )
        log.info("Position closed (%s) — firing immediate post-close LLM cycle", labels)
        self._cancel_cooldown_timer()
        self._last_watch_cycle_utc = None  # clear watch grace so cycle runs immediately
        self._run_cycle(trigger="post_close")

    # ------------------------------------------------------------------
    # Position monitor daemon (IB poll + fill timeouts + hold expiry)
    # ------------------------------------------------------------------

    _POSITION_POLL_INTERVAL = 15  # seconds between IB position checks

    def _position_expiry_loop(self) -> None:
        """Poll every 15 s; detect fills/exits, handle fill timeouts, hold expiry.

        This is the single thread responsible for all IB position lifecycle
        transitions.  It runs at the same cadence as the UI position banner
        (15 s) so state changes appear promptly in the GUI.
        """
        if self._stop_event.wait(timeout=15):  # startup delay
            return
        while not self._stop_event.is_set():
            try:
                from drift.utils.config import load_app_config
                config = load_app_config(self._config_path)
                # 1. Poll IB for fills and exits (SL/TP hits)
                self._poll_positions(config)
                # 2. Check fill timeouts and thesis window expiry
                self._handle_fill_timeouts()
                self._close_expired_positions()
            except Exception as exc:  # noqa: BLE001
                log.warning("Position monitor error: %s", exc)
            self._stop_event.wait(timeout=self._POSITION_POLL_INTERVAL)

    def _handle_fill_timeouts(self) -> None:
        """Auto-trigger Assess for WORKING orders that exceed fill_timeout_minutes.

        When a WORKING order has been pending fill for longer than the configured
        ``fill_timeout_minutes``, an automatic assessment is triggered.  The result
        is stored in session state so the position banner shows it to the operator.

        Also enforces the thesis-window hard stop: if signal_time + max_hold_minutes
        has elapsed and the order is still WORKING, auto-cancel unconditionally
        and fire an immediate post-close LLM cycle.
        """
        from drift.brokers.position_manager import PositionManager
        from drift.gui.state import _PROJECT_ROOT
        from drift.storage.trade_store import TradeStore
        from drift.utils.config import load_app_config

        config = load_app_config(self._config_path)
        root = _PROJECT_ROOT
        db_path = str(root / config.storage.sqlite_path)

        store = TradeStore(db_path)
        working_trades = store.get_working()
        store.close()

        if not working_trades:
            return

        now = datetime.now(tz=timezone.utc)
        fill_timeout = config.risk.fill_timeout_minutes
        cancelled_any = False

        for pos in working_trades:
            # Use thesis_anchor (= generated_at initially, reset on assess)
            anchor_str = pos.thesis_anchor or pos.generated_at
            if not anchor_str:
                continue
            try:
                anchor_dt = datetime.fromisoformat(anchor_str)
                if anchor_dt.tzinfo is None:
                    anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            elapsed_min = (now - anchor_dt).total_seconds() / 60

            # HARD STOP: thesis window expired → auto-cancel regardless
            if elapsed_min >= pos.max_hold_minutes:
                log.info(
                    "Trade %d WORKING — thesis window expired (%.0f min >= %d min) — auto-cancelling",
                    pos.id, elapsed_min, pos.max_hold_minutes,
                )
                mgr = PositionManager(config, db_path)
                result = mgr.manual_close(pos.id)
                mgr.close()
                if result["status"] == "ok":
                    log.info("Trade %d auto-cancelled at thesis window expiry", pos.id)
                    cancelled_any = True
                else:
                    log.error("Failed to auto-cancel trade %d: %s", pos.id, result.get("message"))
                continue

            # FILL TIMEOUT: trigger auto-assess if not already pending
            if elapsed_min >= fill_timeout:
                assess_key = f"fill_timeout_assessed_{pos.id}"
                if assess_key not in self._fill_timeout_assessed:
                    log.info(
                        "Trade %d WORKING — fill timeout (%.0f min >= %d min) — triggering auto-assess",
                        pos.id, elapsed_min, fill_timeout,
                    )
                    self._fill_timeout_assessed.add(assess_key)
                    self._auto_assess_position(config, pos)

        if cancelled_any:
            self._queue_post_close_cycle([{"type": "THESIS_WINDOW_CANCEL", "position_id": 0}])

    def _auto_assess_position(self, config, pos) -> None:
        """Run an automatic LLM assessment for a position and store the result."""
        try:
            from drift.ai.position_advisor import assess_position
            from drift.brokers.position_manager import PositionManager
            from drift.gui.state import _PROJECT_ROOT

            db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
            rec = assess_position(config, pos)

            mgr = PositionManager(config, db_path)
            assess_id = mgr.log_assessment(pos.id, rec)
            mgr.close()

            # Store in session state so the position banner picks it up
            import streamlit as st
            st.session_state[f"bn_assess_result_{pos.id}"] = {
                "rec": rec,
                "assess_id": assess_id,
            }
            log.info(
                "Auto-assess trade %d: %s (%d%% confidence) — %s",
                pos.id, rec.action, rec.confidence, rec.rationale[:80],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-assess failed for trade %d: %s", pos.id, exc)

    def _close_expired_positions(self) -> None:
        """Close any FILLED trade whose thesis window has expired.

        The thesis window is measured from ``thesis_anchor`` (which defaults to
        ``generated_at`` — the signal time — but resets when an Assess updates
        ``max_hold_minutes``).

        MANUAL exit mode is exempt — the operator explicitly chose to hold
        indefinitely, so the thesis window is informational only.

        After a successful auto-close, fires an immediate post-close LLM cycle.
        """
        from drift.brokers.position_manager import PositionManager
        from drift.gui.state import _PROJECT_ROOT
        from drift.storage.trade_store import TradeStore
        from drift.utils.config import load_app_config

        config = load_app_config(self._config_path)
        root = _PROJECT_ROOT
        db_path = str(root / config.storage.sqlite_path)

        store = TradeStore(db_path)
        filled_trades = store.get_filled()
        store.close()

        closed_any = False
        now = datetime.now(tz=timezone.utc)
        for pos in filled_trades:
            # MANUAL = hold indefinitely; skip it
            if pos.exit_mode == "MANUAL":
                continue
            # Use thesis_anchor (= generated_at initially, reset on assess)
            anchor_str = pos.thesis_anchor or pos.generated_at
            if not anchor_str or not pos.max_hold_minutes:
                continue
            try:
                anchor_dt = datetime.fromisoformat(anchor_str)
                if anchor_dt.tzinfo is None:
                    anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
                elapsed_min = (now - anchor_dt).total_seconds() / 60
                if elapsed_min < pos.max_hold_minutes:
                    continue
            except (ValueError, TypeError):
                continue

            log.info(
                "Trade %d %s expired (%.0f min >= %d min) — auto-closing",
                pos.id, pos.exit_mode, elapsed_min, pos.max_hold_minutes,
            )
            mgr = PositionManager(config, db_path)
            result = mgr.manual_close(pos.id)
            mgr.close()
            if result["status"] == "ok":
                log.info("Trade %d auto-closed at hold window expiry", pos.id)
                closed_any = True
            else:
                log.error(
                    "Failed to auto-close trade %d: %s",
                    pos.id, result.get("message"),
                )

        if closed_any:
            self._queue_post_close_cycle([{"type": "HOLD_EXPIRY", "position_id": 0}])


# ---------------------------------------------------------------------------
# Watch condition helpers
# ---------------------------------------------------------------------------

def _condition_met(
    condition_type: str,
    value: float,
    last_price: float,
    current_rsi: float | None,
) -> bool:
    if condition_type == "price_above":
        return last_price >= value
    if condition_type == "price_below":
        return last_price <= value
    if condition_type == "rsi_above" and current_rsi is not None:
        return current_rsi >= value
    if condition_type == "rsi_below" and current_rsi is not None:
        return current_rsi <= value
    return False


def _trade_plan_level_hit(plan: object, price: float) -> bool:
    """Return True if price has reached the active plan's stop loss, TP1, or TP2."""
    bias = (getattr(plan, "bias", None) or "LONG").upper()
    sl  = getattr(plan, "stop_loss",     None)
    tp1 = getattr(plan, "take_profit_1", None)
    tp2 = getattr(plan, "take_profit_2", None)
    if bias == "LONG":
        if sl  is not None and price <= sl:  return True  # noqa: E701
        if tp1 is not None and price >= tp1: return True  # noqa: E701
        if tp2 is not None and price >= tp2: return True  # noqa: E701
    else:  # SHORT
        if sl  is not None and price >= sl:  return True  # noqa: E701
        if tp1 is not None and price <= tp1: return True  # noqa: E701
        if tp2 is not None and price <= tp2: return True  # noqa: E701
    return False


def _trade_plan_hit_label(plan: object, price: float) -> str:
    """Human-readable label for which level was crossed (used in log messages)."""
    bias = (getattr(plan, "bias", None) or "LONG").upper()
    sl  = getattr(plan, "stop_loss",     None)
    tp1 = getattr(plan, "take_profit_1", None)
    tp2 = getattr(plan, "take_profit_2", None)
    if bias == "LONG":
        if sl  is not None and price <= sl:  return f"stop loss ({sl:.2f})"
        if tp2 is not None and price >= tp2: return f"TP2 ({tp2:.2f})"
        if tp1 is not None and price >= tp1: return f"TP1 ({tp1:.2f})"
    else:
        if sl  is not None and price >= sl:  return f"stop loss ({sl:.2f})"
        if tp2 is not None and price <= tp2: return f"TP2 ({tp2:.2f})"
        if tp1 is not None and price <= tp1: return f"TP1 ({tp1:.2f})"
    return "unknown level"


def _entry_zone_in_bar_range(plan: object, bars: list) -> bool:
    """Return True if any 1m bar formed *after* the plan was issued overlapped the entry zone.

    Uses OHLC range overlap: a bar touched zone [entry_min, entry_max] when
    ``bar.low <= entry_max`` AND ``bar.high >= entry_min``.  Only bars whose
    ``timestamp`` is >= the plan's issue time are considered to avoid false
    positives from bars that pre-date the signal.
    """
    from drift.models import Bar
    entry_min = getattr(plan, "entry_min", None)
    entry_max = getattr(plan, "entry_max", None)
    if entry_min is None or entry_max is None:
        return False
    plan_time = getattr(plan, "event_time", None)  # tz-aware datetime
    for bar in bars:
        if not isinstance(bar, Bar):
            continue
        if plan_time is not None and bar.timestamp < plan_time:
            continue
        if bar.low <= entry_max and bar.high >= entry_min:
            return True
    return False


def _classify_resolved_outcome(
    plan: object, price: float, zone_touched: bool
) -> tuple[str, float]:
    """Determine the outcome label and pnl_points when a TP/SL level is breached.

    pnl is measured in price points relative to the midpoint of the entry zone.
    Returns ``(outcome_label, pnl_points)``.

    Outcome labels:
    - ``TP2_HIT``      — entry zone touched; price hit TP2 (best result)
    - ``TP1_HIT``      — entry zone touched; price hit TP1
    - ``STOP_HIT``     — entry zone touched; price hit the stop loss (negative pnl)
    - ``ENTRY_MISSED`` — TP/SL triggered but entry zone was never touched; no fill
    """
    if not zone_touched:
        return "ENTRY_MISSED", 0.0

    bias = (getattr(plan, "bias", None) or "LONG").upper()
    entry_min = getattr(plan, "entry_min", None) or 0.0
    entry_max = getattr(plan, "entry_max", None) or 0.0
    entry_mid = (entry_min + entry_max) / 2.0
    sl  = getattr(plan, "stop_loss",     None)
    tp1 = getattr(plan, "take_profit_1", None)
    tp2 = getattr(plan, "take_profit_2", None)

    if bias == "LONG":
        if tp2 is not None and price >= tp2:
            return "TP2_HIT", round(tp2 - entry_mid, 2)
        if tp1 is not None and price >= tp1:
            return "TP1_HIT", round(tp1 - entry_mid, 2)
        if sl  is not None and price <= sl:
            return "STOP_HIT", round(sl - entry_mid, 2)  # negative
    else:  # SHORT
        if tp2 is not None and price <= tp2:
            return "TP2_HIT", round(entry_mid - tp2, 2)
        if tp1 is not None and price <= tp1:
            return "TP1_HIT", round(entry_mid - tp1, 2)
        if sl  is not None and price >= sl:
            return "STOP_HIT", round(entry_mid - sl, 2)  # negative

    return "EXPIRED", 0.0


def _compute_rsi(bars: list) -> float | None:
    """Compute 14-period RSI from bar close prices. Returns None if insufficient data."""
    if len(bars) < 15:
        return None
    closes = [b.close for b in bars]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    for i in range(14, len(deltas)):
        avg_gain = (avg_gain * 13 + gains[i]) / 14
        avg_loss = (avg_loss * 13 + losses[i]) / 14
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@st.cache_resource
def _get_scheduler(config_path: str, loop_interval_seconds: int) -> BackgroundScheduler:
    """Create exactly one BackgroundScheduler for the lifetime of the process."""
    return BackgroundScheduler(config_path, loop_interval_seconds)


def ensure_scheduler_running() -> BackgroundScheduler:
    """Called once from app.py; returns the running scheduler singleton."""
    from drift.gui.state import _DEFAULT_CONFIG, _PROJECT_ROOT
    from drift.utils.config import load_app_config
    import os

    config_path = os.environ.get("DRIFT_CONFIG", _DEFAULT_CONFIG)

    # config_path may be relative (e.g. "config/settings.yaml" set by the CLI).
    # Always resolve against the known project root so the scheduler thread
    # finds the file regardless of what directory Streamlit is running from.
    abs_config_path = str(
        Path(config_path) if Path(config_path).is_absolute()
        else _PROJECT_ROOT / config_path
    )

    config   = load_app_config(abs_config_path)
    interval = config.app.loop_interval_seconds

    return _get_scheduler(abs_config_path, interval)


def restart_scheduler() -> BackgroundScheduler:
    """Stop the current scheduler and start a fresh one.

    Called from the controls page when the operator switches the active
    instrument.  The old daemon threads are signalled to exit at their next
    sleep checkpoint; the Streamlit cache is cleared so the next call to
    ``ensure_scheduler_running()`` creates a new ``BackgroundScheduler``
    instance that picks up the latest ``active_instrument.json`` override.
    """
    # Signal current instance to stop (if one exists).
    try:
        current = ensure_scheduler_running()
        current.stop()
    except Exception:  # noqa: BLE001
        pass

    # Drop the cached instance so the next call builds a fresh one.
    _get_scheduler.clear()

    # Create and return the new scheduler instance.
    return ensure_scheduler_running()
