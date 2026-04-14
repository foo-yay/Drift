from __future__ import annotations

from abc import ABC, abstractmethod

from drift.models import Bar


class MarketDataProvider(ABC):
    @abstractmethod
    def get_latest_quote(self, symbol: str) -> float:
        """Return the latest quote for the given symbol."""

    @abstractmethod
    def get_recent_bars(self, symbol: str, timeframe: str, lookback: int) -> list[Bar]:
        """Return recent bars for the requested timeframe."""

    @abstractmethod
    def get_session_status(self, symbol: str) -> str:
        """Return a session status label for the instrument."""

    @abstractmethod
    def is_market_open(self, symbol: str) -> bool:
        """Return whether the market is currently open."""

