from pathlib import Path

import json
import pytest

from drift.config.models import AppConfig, InstrumentSection
from drift.utils.config import load_app_config


def test_load_default_config() -> None:
    config = load_app_config("config/settings.yaml")

    assert isinstance(config, AppConfig)
    assert config.app.name == "Drift"
    assert config.app.mode == "paper-live"
    assert config.instrument.symbol == "MNQ"


def test_instrument_section_new_fields() -> None:
    config = load_app_config("config/settings.yaml")
    inst = config.instrument

    assert inst.asset_class == "futures"
    assert inst.tick_value == 0.50
    assert inst.yfinance_symbol == "NQ=F"
    assert inst.exchange == "CME"
    assert inst.currency == "USD"


def test_watched_instruments_loaded() -> None:
    config = load_app_config("config/settings.yaml")

    syms = [i.symbol for i in config.watched_instruments]
    assert "MNQ" in syms
    assert "SPY" in syms
    assert "QQQ" in syms

    spy = next(i for i in config.watched_instruments if i.symbol == "SPY")
    assert spy.asset_class == "equity"
    assert spy.tick_value == 1.0
    assert spy.exchange == "SMART"
    assert spy.allow_short is False


def test_active_instrument_override(tmp_path) -> None:
    """Writing active_instrument.json next to settings.yaml overrides config.instrument."""
    import shutil

    # Copy settings.yaml into a temp directory so we don't touch real files.
    src = Path("config/settings.yaml")
    tmp_cfg = tmp_path / "settings.yaml"
    shutil.copy(src, tmp_cfg)

    # Write an active_instrument.json that switches to SPY.
    (tmp_path / "active_instrument.json").write_text(
        json.dumps({"symbol": "SPY"}), encoding="utf-8"
    )

    config = load_app_config(tmp_cfg)
    assert config.instrument.symbol == "SPY"
    assert config.instrument.asset_class == "equity"


def test_active_instrument_override_unknown_symbol_is_ignored(tmp_path) -> None:
    """An unrecognised symbol in active_instrument.json leaves the default intact."""
    import shutil

    src = Path("config/settings.yaml")
    tmp_cfg = tmp_path / "settings.yaml"
    shutil.copy(src, tmp_cfg)

    (tmp_path / "active_instrument.json").write_text(
        json.dumps({"symbol": "UNKNOWN"}), encoding="utf-8"
    )

    config = load_app_config(tmp_cfg)
    assert config.instrument.symbol == "MNQ"  # unchanged


def test_active_instrument_override_custom_full_profile(tmp_path) -> None:
    """A full profile in active_instrument.json constructs an InstrumentSection
    for symbols not listed in watched_instruments (e.g. NVDA)."""
    import shutil

    src = Path("config/settings.yaml")
    tmp_cfg = tmp_path / "settings.yaml"
    shutil.copy(src, tmp_cfg)

    nvda_profile = {
        "symbol": "NVDA",
        "asset_class": "equity",
        "tick_value": 1.0,
        "yfinance_symbol": "NVDA",
        "exchange": "SMART",
        "currency": "USD",
        "allow_long": True,
        "allow_short": False,
    }
    (tmp_path / "active_instrument.json").write_text(
        json.dumps(nvda_profile), encoding="utf-8"
    )

    config = load_app_config(tmp_cfg)
    assert config.instrument.symbol == "NVDA"
    assert config.instrument.asset_class == "equity"
    assert config.instrument.exchange == "SMART"
    assert config.instrument.tick_value == 1.0


def test_missing_config_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_app_config(Path("config/does-not-exist.yaml"))


def test_instrument_section_defaults() -> None:
    """InstrumentSection defaults allow minimal YAML with only required fields."""
    inst = InstrumentSection(symbol="FOO", allow_long=True, allow_short=False)
    assert inst.asset_class == "futures"
    assert inst.tick_value == 0.50
    assert inst.yfinance_symbol is None
    assert inst.exchange == "CME"
    assert inst.currency == "USD"

