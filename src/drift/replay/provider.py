"""ReplayProvider — MarketDataProvider backed by pre-loaded historical bars.

Implements the same interface as YFinanceProvider so that FeatureEngine and
all gates can be used without modification during replay.

The provider holds *all* bars for a session and exposes a cursor that
advances one 1m bar at a time.  On each step ``advance()`` is called to move
the cursor forward; ``get_recent_bars`` then returns only bars whose timestamp
is at or before the current cursor timestamp (simulating what a live provider
would return at that moment in time).
"""
from __future__ import annotations

from datetime import datetime, timezone

from drift.data.providers.base import MarketDataProvider
from drift.models import Bar


def _session_label(ts: datetime) -> str:
    """Classify a UTC timestamp into a session label."""
    # Convert to ET for session boundary checks.
    # Simple UTC-based approximation — ET is UTC-4 (EDT) or UTC-5 (EST).
    # For replay purposes a rough label is sufficient.
    hour_et = (ts.hour - 4) % 24  # assume EDT
    if 9 <= hour_et < 16:
        return "RTH"
    if 18 <= hour_et < 24 or 0 <= hour_et < 4:
        return "OVERNIGHT"
    return "PRE/POST"


class ReplayProvider(MarketDataProvider):
    """Serves historical bars up to the current replay cursor position.

    Args:
        bars_1m:  All 1m bars for the replay period, oldest-first.
        bars_5m:  All 5m bars for the replay period, oldest-first.
        bars_1h:  All 1h bars for the replay period, oldest-first.
        symbol:   Instrument symbol (passed through, not validated).
    """

    def __init__(
        self,
        bars_1m: list[Bar],
        bars_5m: list[Bar],
        bars_1h: list[Bar],
        symbol: str,
    ) -> None:
        if not bars_1m:
            raise ValueError("bars_1m cannot be empty")
        # Ensure oldest-first ordering
        self._bars_1m = sorted(bars_1m, key=lambda b: b.timestamp)
        self._bars_5m = sorted(bars_5m, key=lambda b: b.timestamp)
        self._bars_1h = sorted(bars_1h, key=lambda b: b.timestamp)
        self._symbol = symbol
        # Cursor starts at index 0 — caller must call advance() before first use.
        self._cursor: int = 0

    # ------------------------------------------------------------------
    # Cursor control
    # ------------------------------------------------------------------

    @property
    def current_timestamp(self) -> datetime:
        return self._bars_1m[self._cursor].timestamp

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def total_steps(self) -> int:
        return len(self._bars_1m)

    def has_next(self) -> bool:
        return self._cursor < len(self._bars_1m) - 1

    def advance(self) -> bool:
        """Move the cursor forward by one 1m bar.

        Returns:
            True if the cursor was advanced; False if already at the last bar.
        """
        if self._cursor < len(self._bars_1m) - 1:
            self._cursor += 1
            return True
        return False

    # ------------------------------------------------------------------
    # MarketDataProvider interface
    # ------------------------------------------------------------------

    def get_latest_quote(self, symbol: str) -> float:
        return self._bars_1m[self._cursor].close

    def get_recent_bars(self, symbol: str, timeframe: str, lookback: int) -> list[Bar]:
        """Return up to *lookback* bars whose timestamp ≤ cursor timestamp."""
        cutoff = self.current_timestamp
        if timeframe == "1m":
            source = self._bars_1m
        elif timeframe == "5m":
            source = self._bars_5m
        elif timeframe == "1h":
            source = self._bars_1h
        else:
            raise ValueError(f"Unsupported timeframe for replay: {timeframe!r}")

        visible = [b for b in source if b.timestamp <= cutoff]
        return visible[-lookback:]

    def get_session_status(self, symbol: str) -> str:
        return _session_label(self.current_timestamp)

    def is_market_open(self, symbol: str) -> bool:
        return self.get_session_status(symbol) == "RTH"
