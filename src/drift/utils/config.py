from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

from drift.config.models import AppConfig


def _resolve_config_path(config_path: Path) -> Path:
    """Resolve a config path that may be relative.

    If the path does not exist relative to CWD, walk up the directory tree
    looking for the same relative path. This lets `drift run` work correctly
    whether invoked from the project root, a subdirectory, or via an IDE run
    configuration with a different working directory.
    """
    if config_path.is_absolute() or config_path.exists():
        return config_path

    # Walk up through parent directories
    candidate = Path.cwd()
    for _ in range(10):  # guard against infinite loop at filesystem root
        target = candidate / config_path
        if target.exists():
            return target
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    return config_path  # return original so the error message is meaningful


def load_app_config(config_path: str | Path) -> AppConfig:
    path = _resolve_config_path(Path(config_path))
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Run 'drift' from the project root, or pass --config with an absolute path."
        )

    load_dotenv(override=False)

    with path.open("r", encoding="utf-8") as file_handle:
        payload = yaml.safe_load(file_handle) or {}

    return AppConfig.model_validate(payload)

