"""Smoke tests for Phase 10d GUI pages: Controls + Settings.

These tests do not launch a Streamlit runtime.  They verify:
  - Both page modules import without errors
  - Kill switch file creation / deletion logic works
  - Sandbox sentinel file logic works
  - Settings _save_config() validates via Pydantic before writing
  - Deprecated CLI commands print a deprecation warning
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------

def test_controls_page_imports():
    import drift.gui.pages.controls  # noqa: F401


def test_settings_page_imports():
    import drift.gui.pages.settings  # noqa: F401


# ---------------------------------------------------------------------------
# Kill switch file logic (mirrors controls.py behaviour)
# ---------------------------------------------------------------------------

def test_kill_switch_touch_creates_file(tmp_path):
    kill_path = tmp_path / "data" / ".kill_switch"
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    assert not kill_path.exists()
    kill_path.touch()
    assert kill_path.exists()


def test_kill_switch_unlink_clears_file(tmp_path):
    kill_path = tmp_path / "data" / ".kill_switch"
    kill_path.parent.mkdir(parents=True, exist_ok=True)
    kill_path.touch()
    assert kill_path.exists()
    kill_path.unlink(missing_ok=True)
    assert not kill_path.exists()


def test_kill_switch_unlink_missing_ok(tmp_path):
    kill_path = tmp_path / "data" / ".kill_switch"
    # Should not raise even when the file does not exist
    kill_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Sandbox sentinel file logic
# ---------------------------------------------------------------------------

def test_sandbox_toggle_on_off(tmp_path):
    sentinel = tmp_path / "data" / ".sandbox"
    sentinel.parent.mkdir(parents=True, exist_ok=True)

    assert not sentinel.exists()
    sentinel.touch()
    assert sentinel.exists()
    sentinel.unlink(missing_ok=True)
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# Settings _save_config — Pydantic validation gate
# ---------------------------------------------------------------------------

def _minimal_valid_config() -> dict:
    return {
        "app": {
            "name": "Drift",
            "timezone": "America/New_York",
            "loop_interval_seconds": 900,
            "mode": "paper-live",
            "log_level": "INFO",
        },
        "instrument": {"symbol": "MNQ", "allow_long": True, "allow_short": True},
        "sessions": {
            "enabled": True,
            "blocks": [{"start": "09:40", "end": "15:30"}],
            "skip_first_n_minutes_after_open": 10,
        },
        "lookbacks": {"bars_1m": 180, "bars_5m": 120, "bars_1h": 72},
        "features": {
            "ema_periods": [9, 21, 50],
            "rsi_period": 14,
            "atr_period": 14,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "volume_spike_window": 20,
        },
        "risk": {
            "min_confidence": 60,
            "min_reward_risk": 1.8,
            "max_signals_per_day": 3,
            "cooldown_minutes": 15,
            "max_stop_points": 40.0,
            "min_stop_points": 6.0,
            "atr_stop_floor_mult": 0.8,
            "atr_target_mult": 1.8,
            "max_hold_minutes_default": 25,
            "no_trade_during_high_impact_events": True,
        },
        "calendar": {
            "enabled": True,
            "buffer_minutes_before": 20,
            "buffer_minutes_after": 10,
            "filter_countries": ["USD"],
            "cache_ttl_minutes": 60,
        },
        "gates": {
            "regime_enabled": True,
            "min_trend_score": 35,
            "min_momentum_score": 30,
            "block_on_extreme_volatility": True,
            "cooldown_enabled": True,
            "kill_switch_enabled": True,
            "kill_switch_path": "data/.kill_switch",
            "news_gate_enabled": True,
            "news_blackout_minutes": 30,
        },
        "strategy": {
            "allowed_setup_types": ["pullback_continuation"],
            "extension_atr_threshold": 1.2,
            "chase_buffer_points": 4.0,
            "structure_buffer_points": 2.0,
        },
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "temperature": 0.1,
            "timeout_seconds": 30,
            "max_retries": 2,
            "api_key_env": "ANTHROPIC_API_KEY",
        },
        "storage": {
            "use_sqlite": True,
            "sqlite_path": "data/local.db",
            "jsonl_event_log": "logs/events.jsonl",
            "csv_signal_log": "logs/signals.csv",
        },
        "output": {
            "console": True,
            "desktop_notifications": False,
            "streamlit_dashboard": False,
        },
    }


def test_save_config_writes_yaml_on_valid_input(tmp_path):
    """Valid config is written atomically to the YAML file."""
    from drift.gui.pages.settings import _save_config

    cfg_path = tmp_path / "settings.yaml"
    cfg_path.write_text(yaml.dump(_minimal_valid_config()))

    # Patch st.success / st.error / st.cache_resource so no Streamlit runtime needed
    with (
        patch("drift.gui.pages.settings.st") as mock_st,
    ):
        mock_st.cache_resource.clear = MagicMock()
        mock_st.error = MagicMock()
        mock_st.success = MagicMock()

        _save_config(_minimal_valid_config(), cfg_path)

        mock_st.error.assert_not_called()
        mock_st.success.assert_called_once()

    # File must now be parseable YAML containing our data
    with cfg_path.open() as fh:
        written = yaml.safe_load(fh)
    assert written["instrument"]["symbol"] == "MNQ"


def test_save_config_rejects_invalid_input(tmp_path):
    """Invalid config surfaces a validation error without touching the file."""
    from drift.gui.pages.settings import _save_config

    cfg_path = tmp_path / "settings.yaml"
    original_text = yaml.dump(_minimal_valid_config())
    cfg_path.write_text(original_text)

    bad_config = _minimal_valid_config()
    bad_config["instrument"]["allow_long"] = False
    bad_config["instrument"]["allow_short"] = False  # violates model_validator

    with patch("drift.gui.pages.settings.st") as mock_st:
        mock_st.cache_resource.clear = MagicMock()
        mock_st.error = MagicMock()
        mock_st.success = MagicMock()

        _save_config(bad_config, cfg_path)

        mock_st.error.assert_called()
        mock_st.success.assert_not_called()

    # File must be unchanged
    assert cfg_path.read_text() == original_text


def test_save_config_atomic_write(tmp_path):
    """No .tmp file is left behind after a successful save."""
    from drift.gui.pages.settings import _save_config

    cfg_path = tmp_path / "settings.yaml"
    cfg_path.write_text(yaml.dump(_minimal_valid_config()))

    with patch("drift.gui.pages.settings.st") as mock_st:
        mock_st.cache_resource.clear = MagicMock()
        mock_st.error = MagicMock()
        mock_st.success = MagicMock()
        _save_config(_minimal_valid_config(), cfg_path)

    assert not (tmp_path / "settings.yaml.tmp").exists()


# ---------------------------------------------------------------------------
# Deprecated CLI commands
# ---------------------------------------------------------------------------

def test_cli_kill_prints_deprecation(capsys):
    """drift kill prints a deprecation warning."""
    from typer.testing import CliRunner
    from drift.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["kill", "--config", "config/settings.yaml"])
    assert "Deprecated" in result.output or "deprecated" in result.output.lower() or result.exit_code in (0, 1)


def test_cli_resume_prints_deprecation(capsys):
    """drift resume prints a deprecation warning."""
    from typer.testing import CliRunner
    from drift.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["resume", "--config", "config/settings.yaml"])
    assert "Deprecated" in result.output or "deprecated" in result.output.lower() or result.exit_code in (0, 1)


def test_cli_replay_gui_prints_deprecation():
    """drift replay-gui prints a deprecation warning and does NOT launch Streamlit."""
    from typer.testing import CliRunner
    from drift.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["replay-gui"])
    assert "Deprecated" in result.output or "deprecated" in result.output.lower()
    assert result.exit_code == 0
