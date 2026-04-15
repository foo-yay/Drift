from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

# yfinance uses pd.Timestamp.utcnow() which is deprecated in pandas 4.
# Suppress until yfinance ships a fix.  Omit category= because pandas 4
# emits Pandas4Warning which is not guaranteed to subclass FutureWarning.
warnings.filterwarnings("ignore", message="Timestamp.utcnow")

from drift.data.providers.base import MarketDataProvider
from drift.models import Bar

_ET = ZoneInfo("America/New_York")

# Map internal symbol names to yfinance tickers.
# MNQ (Micro E-mini NASDAQ-100) is not directly available via yfinance;
# NQ=F (E-mini NASDAQ-100 futures) is used as a price proxy.
_SYMBOL_MAP: dict[str, str] = {
    "MNQ": "NQ=F",
    "MES": "ES=F",
    "ES": "ES=F",
}

_TIMEFRAME_TO_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "1h": "1h",
}

# Trading minutes per bar, used to estimate how many calendar days to fetch.
_MINUTES_PER_BAR: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "1h": 60,
}

# yfinance 1m data is only available for the past 7 calendar days.
_MAX_DAYS_FOR_INTERVAL: dict[str, int] = {
    "1m": 7,
    "5m": 60,
    "1h": 730,
}


def _resolve_ticker(symbol: str) -> str:
    return _SYMBOL_MAP.get(symbol.upper(), symbol)


def _calendar_days_needed(timeframe: str, lookback: int) -> int:
    minutes_per_trading_day = 390  # ~6.5 hours of RTH
    minutes_needed = _MINUTES_PER_BAR[timeframe] * lookback
    raw = max(2, int(minutes_needed / minutes_per_trading_day) + 5)
    return min(raw, _MAX_DAYS_FOR_INTERVAL[timeframe])


class YFinanceProvider(MarketDataProvider):
    """Market data provider backed by yfinance (free, delayed data).

    yfinance returns delayed quotes (~15 min for most futures proxies).
    Suitable for dry-run validation and paper-live prototyping.
    Switch to a live feed (Alpaca, Interactive Brokers, etc.) for real signals.
    """

    def _get_ticker(self, symbol: str) -> yf.Ticker:
        return yf.Ticker(_resolve_ticker(symbol))

    def get_latest_quote(self, symbol: str) -> float:
        ticker = self._get_ticker(symbol)
        price: float | None = ticker.fast_info.last_price
        if price is None or price <= 0:
            mapped = _resolve_ticker(symbol)
            raise ValueError(
                f"Could not retrieve a valid price for {symbol!r} "
                f"(yfinance ticker: {mapped!r}). "
                "Check that the symbol is correct and the market is not closed."
            )
        return float(price)

    def get_recent_bars(self, symbol: str, timeframe: str, lookback: int) -> list[Bar]:
        if timeframe not in _TIMEFRAME_TO_INTERVAL:
            raise ValueError(
                f"Unsupported timeframe: {timeframe!r}. "
                f"Must be one of {list(_TIMEFRAME_TO_INTERVAL)}."
            )

        days = _calendar_days_needed(timeframe, lookback)
        ticker = self._get_ticker(symbol)

        end_dt = datetime.now(tz=timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        df = ticker.history(
            start=start_dt,
            end=end_dt,
            interval=_TIMEFRAME_TO_INTERVAL[timeframe],
            auto_adjust=True,
        )

        if df is None or df.empty:
            return []

        bars: list[Bar] = []
        for ts, row in df.tail(lookback).iterrows():
            dt = ts.to_pydatetime()
            dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            try:
                bars.append(
                    Bar(
                        timestamp=dt,
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=float(row["Volume"]),
                        timeframe=timeframe,
                        symbol=symbol,
                    )
                )
            except (ValueError, KeyError):
                # Skip individual malformed bars rather than failing the entire fetch.
                continue

        return bars

    def get_session_status(self, symbol: str) -> str:  # noqa: ARG002
        """Return a simple session label based on US Eastern time."""
        now_et = datetime.now(tz=_ET)
        minutes = now_et.hour * 60 + now_et.minute
        if 9 * 60 + 30 <= minutes < 16 * 60:
            return "open"
        if 4 * 60 <= minutes < 9 * 60 + 30:
            return "pre-market"
        return "after-hours"

    def is_market_open(self, symbol: str) -> bool:
        return self.get_session_status(symbol) == "open"
