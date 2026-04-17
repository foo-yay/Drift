"""Auto-start IB Gateway via IBC when the broker is needed.

When broker.auto_start_gateway is true in settings.yaml, any call to
IBClient._connect() will first ensure IB Gateway is listening on the
configured port.  If it is not, the IBC start script is launched in the
background and we wait for the port to open (up to
GATEWAY_START_TIMEOUT_SECS seconds).

The script is run with the -inline flag so it does not open a new window.
stdout/stderr are written to a log file inside the IBC folder.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GATEWAY_START_TIMEOUT_SECS = 90   # IB Gateway can be slow to start
POLL_INTERVAL_SECS = 2

# Module-level handle so we only launch once per process lifetime.
_gateway_proc: subprocess.Popen | None = None


def _port_open(host: str, port: int) -> bool:
    """Return True if something is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def ensure_gateway_running(broker_cfg: Any) -> None:
    """Guarantee IB Gateway is listening on the configured port.

    If it is already up, this is a fast no-op (a single socket probe).
    If it is not up and auto_start_gateway is enabled, the IBC script is
    launched and we block until the port opens or the timeout expires.

    Args:
        broker_cfg: BrokerSection from AppConfig

    Raises:
        RuntimeError: if Gateway does not start within the timeout.
    """
    global _gateway_proc  # noqa: PLW0603

    host = broker_cfg.host
    port = broker_cfg.port

    if _port_open(host, port):
        log.debug("IB Gateway already listening on %s:%d.", host, port)
        return

    if not getattr(broker_cfg, "auto_start_gateway", False):
        return  # user disabled auto-start; let _connect() fail naturally

    script = getattr(broker_cfg, "gateway_script", "")
    if not script:
        log.warning(
            "broker.auto_start_gateway is true but broker.gateway_script is "
            "not set.  Cannot auto-start IB Gateway."
        )
        return

    script_path = Path(script).expanduser()
    if not script_path.exists():
        raise RuntimeError(
            f"IBC start script not found: {script_path}\n"
            "Set broker.gateway_script in settings.yaml to the full path of "
            "gatewaystartmacos.sh."
        )

    if not os.access(script_path, os.X_OK):
        log.info("Making %s executable.", script_path)
        script_path.chmod(script_path.stat().st_mode | 0o755)

    log.info("IB Gateway not running — launching IBC: %s", script_path)

    log_dir = script_path.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    launch_log = log_dir / "gateway_launch.log"

    with open(launch_log, "a") as fh:
        _gateway_proc = subprocess.Popen(
            [str(script_path), "-inline"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            cwd=str(script_path.parent),
            # Detach from our process group so it survives if we restart
            start_new_session=True,
        )

    log.info(
        "IBC launched (pid=%d). Waiting up to %ds for port %d...",
        _gateway_proc.pid, GATEWAY_START_TIMEOUT_SECS, port,
    )

    deadline = time.monotonic() + GATEWAY_START_TIMEOUT_SECS
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECS)
        if _port_open(host, port):
            log.info("IB Gateway is up on port %d.", port)
            # Give Gateway 2 more seconds to fully initialise before we connect.
            time.sleep(2)
            return
        # Check if the process died early
        if _gateway_proc.poll() is not None:
            raise RuntimeError(
                f"IBC process exited (code {_gateway_proc.returncode}) before "
                f"Gateway became available.  Check: {launch_log}"
            )

    raise RuntimeError(
        f"IB Gateway did not open port {port} within "
        f"{GATEWAY_START_TIMEOUT_SECS}s.  Check: {launch_log}"
    )
