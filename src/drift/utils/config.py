from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

from drift.config.models import AppConfig


def load_app_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    load_dotenv(override=False)

    with path.open("r", encoding="utf-8") as file_handle:
        payload = yaml.safe_load(file_handle) or {}

    return AppConfig.model_validate(payload)

