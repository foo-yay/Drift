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

    config = AppConfig.model_validate(payload)

    # Apply active_instrument.json override — allows the GUI controls page to
    # switch instruments without editing settings.yaml.
    _apply_active_instrument_override(config, path.parent)

    return config


def _apply_active_instrument_override(config: AppConfig, config_dir: Path) -> None:
    """If config_dir/active_instrument.json exists and names a watched instrument,
    replace config.instrument in-place with that instrument's profile.

    Modifies the config object in-place via __dict__ to bypass Pydantic's frozen
    model protection (AppConfig is not frozen, so direct attribute assignment works).
    """
    import json

    override_path = config_dir / "active_instrument.json"
    if not override_path.exists():
        return

    try:
        data = json.loads(override_path.read_text(encoding="utf-8"))
        target_symbol = (data.get("symbol") or "").strip().upper()
    except Exception:  # noqa: BLE001
        return

    if not target_symbol:
        return

    # If it matches the default instrument, nothing to do.
    if config.instrument.symbol.upper() == target_symbol:
        return

    # Search watched_instruments for a matching profile.
    match = next(
        (inst for inst in config.watched_instruments if inst.symbol.upper() == target_symbol),
        None,
    )
    if match is not None:
        object.__setattr__(config, "instrument", match)

