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


class _SchedulerState:
    """Mutable state bag shared between the daemon thread and the GUI."""

    def __init__(self) -> None:
        self.last_run_utc: datetime | None = None
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
        self._thread = threading.Thread(
            target=self._loop,
            name="drift-scheduler",
            daemon=True,         # dies automatically when the process exits
        )
        self._thread.start()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            name="drift-watch-poller",
            daemon=True,
        )
        self._watch_thread.start()
        log.info(
            "BackgroundScheduler started — interval=%ds, watch_poll=%ds",
            loop_interval_seconds, _WATCH_POLL_INTERVAL,
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal loop (daemon thread)
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Run first cycle after a short startup delay, then loop on interval."""
        # Short delay so the process finishes starting up before the first cycle.
        time.sleep(5)
        while True:
            outcome = self._run_cycle()
            if outcome == "BLOCKED":
                self._schedule_cooldown_wakeup()
            else:
                self._cancel_cooldown_timer()
            next_run = datetime.now(tz=timezone.utc) + timedelta(seconds=self._interval)
            self.state.next_run_utc = next_run
            time.sleep(self._interval)

    def _run_cycle(self, watch_trigger: bool = False) -> str:
        from drift.app import DriftApplication
        from drift.gui.state import _PROJECT_ROOT
        from drift.output import console as console_mod
        from drift.utils.config import load_app_config
        from rich.console import Console

        self.state.mark_running()
        buf = io.StringIO()
        capture = Console(file=buf, force_terminal=False, no_color=True, width=100)
        orig = console_mod.console
        console_mod.console = capture
        try:
            config = load_app_config(self._config_path)
            # Absolutize storage paths so the daemon thread always writes to
            # the same files as the GUI reads, regardless of process CWD.
            # Mirrors the pattern used by _run_cycle_now() in controls.py.
            root = _PROJECT_ROOT
            abs_storage = config.storage.model_copy(update={
                "jsonl_event_log":         str(root / config.storage.jsonl_event_log),
                "sqlite_path":             str(root / config.storage.sqlite_path),
                "sandbox_jsonl_event_log": str(root / config.storage.sandbox_jsonl_event_log),
                "sandbox_sqlite_path":     str(root / config.storage.sandbox_sqlite_path),
            })
            abs_config = config.model_copy(update={"storage": abs_storage})
            app = DriftApplication(abs_config, config_path=self._config_path, watch_trigger=watch_trigger)
            outcome = app.run_once() or "unknown"
            self.state.record_run(outcome)
            return outcome
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler cycle error: %s", exc)
            self.state.record_run("error", str(exc))
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
            self._run_cycle()

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
            gate = CooldownGate(config.gates, config.risk, log_path)
            return gate.seconds_remaining()
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not query cooldown remaining: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Watch condition poll loop (every 30s)
    # ------------------------------------------------------------------

    def _watch_loop(self) -> None:
        """Poll active watches every 30 seconds; fire a cycle when one triggers."""
        time.sleep(10)  # short startup delay — let the first scheduled cycle run first
        while True:
            try:
                self._check_watches()
            except Exception as exc:  # noqa: BLE001
                log.warning("Watch poller error: %s", exc)
            time.sleep(_WATCH_POLL_INTERVAL)

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
            self.state.record_watch_trigger()
            self._run_cycle(watch_trigger=True)


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
