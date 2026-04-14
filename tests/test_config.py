from pathlib import Path

import pytest

from drift.config.models import AppConfig
from drift.utils.config import load_app_config


def test_load_default_config() -> None:
    config = load_app_config("config/settings.yaml")

    assert isinstance(config, AppConfig)
    assert config.app.name == "Drift"
    assert config.app.mode == "dry-run"
    assert config.instrument.symbol == "MNQ"


def test_missing_config_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_app_config(Path("config/does-not-exist.yaml"))

