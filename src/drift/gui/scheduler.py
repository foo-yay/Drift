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
    """

    def __init__(self, config_path: str, loop_interval_seconds: int) -> None:
        self._config_path = config_path
        self._interval = loop_interval_seconds
        self.state = _SchedulerState()
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
            self._run_cycle()
            next_run = datetime.now(tz=timezone.utc) + timedelta(seconds=self._interval)
            self.state.next_run_utc = next_run
            time.sleep(self._interval)

    def _run_cycle(self, watch_trigger: bool = False) -> None:
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
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler cycle error: %s", exc)
            self.state.record_run("error", str(exc))
        finally:
            console_mod.console = orig

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
        """Evaluate all active watch conditions; trigger a cycle if any fires."""
        from drift.gui.state import _PROJECT_ROOT
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
        if not active:
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
