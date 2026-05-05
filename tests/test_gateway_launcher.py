"""Tests for gateway_launcher.ensure_gateway_running."""
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from drift.brokers.gateway_launcher import ensure_gateway_running


@dataclass
class _BrokerCfg:
    host: str = "127.0.0.1"
    port: int = 7497
    auto_start_gateway: bool = False
    gateway_script: str = ""


def test_no_op_when_port_already_open():
    """If port is open, function returns immediately without launching anything."""
    cfg = _BrokerCfg()
    with patch("drift.brokers.gateway_launcher._port_open", return_value=True) as mock_probe:
        ensure_gateway_running(cfg)
    mock_probe.assert_called_once()


def test_no_op_when_auto_start_disabled():
    """If port is closed but auto_start_gateway=False, no script is launched."""
    cfg = _BrokerCfg(auto_start_gateway=False)
    with patch("drift.brokers.gateway_launcher._port_open", return_value=False):
        with patch("subprocess.Popen") as mock_popen:
            ensure_gateway_running(cfg)
    mock_popen.assert_not_called()


def test_raises_when_script_not_found(tmp_path):
    """RuntimeError raised when gateway_script path does not exist."""
    cfg = _BrokerCfg(
        auto_start_gateway=True,
        gateway_script=str(tmp_path / "nonexistent.sh"),
    )
    with patch("drift.brokers.gateway_launcher._port_open", return_value=False):
        with pytest.raises(RuntimeError, match="not found"):
            ensure_gateway_running(cfg)


def test_raises_when_no_script_configured():
    """No warning/exception but silent return when script is blank string."""
    cfg = _BrokerCfg(auto_start_gateway=True, gateway_script="")
    # Should return silently (just logs a warning)
    with patch("drift.brokers.gateway_launcher._port_open", return_value=False):
        ensure_gateway_running(cfg)  # must not raise


def test_launches_script_and_waits_for_port(tmp_path):
    """Script is launched and function returns once port opens."""
    script = tmp_path / "gatewaystartmacos.sh"
    script.write_text("#!/bin/bash\nsleep 0\n")
    script.chmod(0o755)

    cfg = _BrokerCfg(
        auto_start_gateway=True,
        gateway_script=str(script),
    )

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.poll.return_value = None  # process still running

    # Port closed on first call, open on second (after "launch")
    port_states = [False, False, True]

    with patch("drift.brokers.gateway_launcher._port_open", side_effect=port_states):
        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("subprocess.run"):
                with patch("drift.brokers.gateway_launcher.POLL_INTERVAL_SECS", 0):
                    with patch("time.sleep"):
                        ensure_gateway_running(cfg)


def test_raises_if_process_exits_early(tmp_path):
    """RuntimeError if the IBC process exits before port opens."""
    script = tmp_path / "gatewaystartmacos.sh"
    script.write_text("#!/bin/bash\nexit 1\n")
    script.chmod(0o755)

    cfg = _BrokerCfg(
        auto_start_gateway=True,
        gateway_script=str(script),
    )

    mock_proc = MagicMock()
    mock_proc.pid = 99
    mock_proc.poll.return_value = 1  # process already dead

    with patch("drift.brokers.gateway_launcher._port_open", return_value=False):
        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("subprocess.run"):
                with patch("drift.brokers.gateway_launcher.POLL_INTERVAL_SECS", 0):
                    with patch("time.sleep"):
                        with pytest.raises(RuntimeError, match="exited"):
                            ensure_gateway_running(cfg)
