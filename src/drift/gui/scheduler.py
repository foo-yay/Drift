"""Background analysis scheduler for the Drift GUI.

Runs ``DriftApplication.run_once()`` on the configured loop interval in a
daemon thread that is independent of any browser connection.  The Streamlit
process is the lifetime boundary — as long as ``drift gui`` is running in
the terminal, cycles execute on schedule whether or not a browser tab is open.

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
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

log = logging.getLogger(__name__)


class _SchedulerState:
    """Mutable state bag shared between the daemon thread and the GUI."""

    def __init__(self) -> None:
        self.last_run_utc: datetime | None = None
        self.last_outcome: str = ""          # "success" | "error" | ""
        self.last_error: str = ""
        self.running: bool = False
        self._lock = threading.Lock()

    # Thread-safe snapshot for the GUI to read.
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "last_run_utc": self.last_run_utc,
                "last_outcome": self.last_outcome,
                "last_error":   self.last_error,
                "running":      self.running,
            }

    def record_run(self, outcome: str, error: str = "") -> None:
        with self._lock:
            self.last_run_utc = datetime.now(tz=timezone.utc)
            self.last_outcome = outcome
            self.last_error   = error
            self.running      = False

    def mark_running(self) -> None:
        with self._lock:
            self.running = True


class BackgroundScheduler:
    """Daemon thread that runs analysis cycles on the configured interval."""

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
        log.info("BackgroundScheduler started — interval=%ds", loop_interval_seconds)

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
            time.sleep(self._interval)

    def _run_cycle(self) -> None:
        from drift.app import DriftApplication
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
            app = DriftApplication(config, config_path=self._config_path)
            app.run_once()
            self.state.record_run("success")
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler cycle error: %s", exc)
            self.state.record_run("error", str(exc))
        finally:
            console_mod.console = orig


# ---------------------------------------------------------------------------
# Streamlit integration — one singleton per server process
# ---------------------------------------------------------------------------

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
    config      = load_app_config(config_path)

    # Absolutize the config path so the scheduler thread always finds it.
    abs_config_path = str(Path(config_path).resolve())
    interval = config.app.loop_interval_seconds

    return _get_scheduler(abs_config_path, interval)
