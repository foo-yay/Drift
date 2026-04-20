"""Shared state and config helpers for the Drift GUI.

Centralises project-root discovery, config loading, and store access so
every page imports from one place instead of re-computing paths.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from drift.config.models import AppConfig
from drift.storage.signal_store import SignalStore
from drift.utils.config import load_app_config

log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Shared live price cache — single fetch, multiple consumers
# ---------------------------------------------------------------------------

_price_cache: dict[str, tuple[float, float]] = {}  # symbol → (price, monotonic_ts)
_price_lock = threading.Lock()
_PRICE_TTL = 5.0  # seconds


def get_live_price(symbol: str) -> float | None:
    """Return the latest quote for *symbol*, cached for 5 seconds.

    Every GUI fragment that needs a live price should call this instead of
    ``YFinanceProvider().get_latest_quote()`` directly.  The first caller within
    a 5-second window triggers the actual Yahoo Finance fetch; all subsequent
    callers receive the cached value.  This guarantees every component on the
    page shows the *same* price and eliminates redundant API calls.

    Returns ``None`` if the fetch fails.
    """
    now = time.monotonic()
    with _price_lock:
        cached = _price_cache.get(symbol)
        if cached is not None:
            price, ts = cached
            if now - ts < _PRICE_TTL:
                return price

    # Outside lock — do the (potentially slow) network fetch.
    try:
        from drift.data.providers.yfinance_provider import YFinanceProvider
        price = YFinanceProvider().get_latest_quote(symbol)
    except Exception:  # noqa: BLE001
        log.debug("Live price fetch failed for %s", symbol)
        # Return stale value if available; else None.
        with _price_lock:
            stale = _price_cache.get(symbol)
            return stale[0] if stale else None

    with _price_lock:
        _price_cache[symbol] = (price, time.monotonic())
    return price
