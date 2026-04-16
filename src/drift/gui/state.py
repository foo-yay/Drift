"""Shared state and config helpers for the Drift GUI.

Centralises project-root discovery, config loading, and store access so
every page imports from one place instead of re-computing paths.
"""
from __future__ import annotations

import os
from pathlib import Path

from drift.config.models import AppConfig
from drift.storage.signal_store import SignalStore
from drift.utils.config import load_app_config

# Project root: src/drift/gui/state.py → 3 levels up → project root
_PROJECT_ROOT = Path(__file__).parents[3]
_DEFAULT_CONFIG = str(_PROJECT_ROOT / "config" / "settings.yaml")


def get_config() -> AppConfig:
    """Load AppConfig from the project config file.

    Respects the ``DRIFT_CONFIG`` env var set by ``drift gui --config``.
    """
    path = os.environ.get("DRIFT_CONFIG", _DEFAULT_CONFIG)
    return load_app_config(path)


def get_store(config: AppConfig) -> SignalStore:
    """Return a ``SignalStore`` pointing at the production database."""
    db_path = _PROJECT_ROOT / config.storage.sqlite_path
    return SignalStore(db_path)


def get_sandbox_store(config: AppConfig) -> SignalStore:
    """Return a ``SignalStore`` pointing at the sandbox database."""
    db_path = _PROJECT_ROOT / config.storage.sandbox_sqlite_path
    return SignalStore(db_path)


def sandbox_sentinel_path() -> Path:
    """Path to the ``.sandbox`` sentinel file that activates sandbox mode."""
    return _PROJECT_ROOT / "data" / ".sandbox"


def project_root() -> Path:
    return _PROJECT_ROOT
