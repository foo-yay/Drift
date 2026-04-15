"""Loader — fetch or read historical bars for replay.

Two entry points:
  - ``load_bars_from_csv``:  Read pre-exported CSVs (one per timeframe).
  - ``fetch_bars_for_date_range``:  Pull from yfinance for a date range.

CSV format (header required):
    timestamp,open,high,low,close,volume

yfinance is used only for data fetching; all returned bars are plain ``Bar``
model instances so the rest of the pipeline is provider-agnostic.
"""
from __future__ import annotations

import csv
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf

# yfinance uses pd.Timestamp.utcnow() which is deprecated in pandas 4.
# This is an upstream bug — suppress until yfinance ships a fix.
warnings.filterwarnings("ignore", message="Timestamp.utcnow", category=FutureWarning)

from drift.data.providers.yfinance_provider import _resolve_ticker
from drift.models import Bar


def _parse_csv_bars(path: Path, timeframe: str, symbol: str) -> list[Bar]:
    """Parse a CSV file into a list of Bar objects.

    Expected columns: timestamp, open, high, low, close, volume
    Timestamps may be ISO-8601 strings or epoch integers (seconds).
    """
    bars: list[Bar] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ts_raw = row["timestamp"].strip()
            # Accept both ISO strings and numeric epoch seconds
            if ts_raw.lstrip("-").isdigit():
                ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

            bars.append(
                Bar(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    timeframe=timeframe,
                    symbol=symbol,
                )
            )
    return sorted(bars, key=lambda b: b.timestamp)


def load_bars_from_csv(
    path_1m: str | Path,
    path_5m: str | Path,
    path_1h: str | Path,
    symbol: str,
) -> tuple[list[Bar], list[Bar], list[Bar]]:
    """Load historical bars from three CSV files (one per timeframe).

    Args:
        path_1m:  Path to the 1-minute bar CSV.
        path_5m:  Path to the 5-minute bar CSV.
        path_1h:  Path to the 1-hour bar CSV.
        symbol:   Instrument symbol to tag bars with.

    Returns:
        (bars_1m, bars_5m, bars_1h) — all sorted oldest-first.
    """
    bars_1m = _parse_csv_bars(Path(path_1m), "1m", symbol)
    bars_5m = _parse_csv_bars(Path(path_5m), "5m", symbol)
    bars_1h = _parse_csv_bars(Path(path_1h), "1h", symbol)
    return bars_1m, bars_5m, bars_1h


def _yf_to_bars(df, timeframe: str, symbol: str) -> list[Bar]:
    """Convert a yfinance DataFrame to a list of Bar objects."""
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        # yfinance returns tz-aware DatetimeIndex
        if hasattr(ts, "to_pydatetime"):
            ts_dt = ts.to_pydatetime()
        else:
            ts_dt = datetime.fromisoformat(str(ts))
        if ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=timezone.utc)

        # Skip rows with NaN OHLC (incomplete bars)
        try:
            bar = Bar(
                timestamp=ts_dt,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
                timeframe=timeframe,
                symbol=symbol,
            )
            bars.append(bar)
        except (ValueError, KeyError):
            continue
    return sorted(bars, key=lambda b: b.timestamp)


def fetch_bars_for_date_range(
    symbol: str,
    start: date | str,
    end: date | str,
) -> tuple[list[Bar], list[Bar], list[Bar]]:
    """Fetch historical bars from yfinance for the given date range.

    Args:
        symbol:  Ticker symbol (e.g. "MNQ=F").
        start:   Start date (inclusive), as a ``date`` or "YYYY-MM-DD" string.
        end:     End date (inclusive), as a ``date`` or "YYYY-MM-DD" string.

    Returns:
        (bars_1m, bars_5m, bars_1h) — all sorted oldest-first.

    Note:
        yfinance 1m data is only available for the last 7 days.
        For older dates use ``load_bars_from_csv`` with pre-downloaded data.
    """
    ticker = yf.Ticker(_resolve_ticker(symbol))

    # yfinance end date is exclusive, so add one day to include the requested end date.
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)
    start_str = str(start)
    end_str = str(end + timedelta(days=1))

    df_1m = ticker.history(interval="1m", start=start_str, end=end_str, auto_adjust=True)
    df_5m = ticker.history(interval="5m", start=start_str, end=end_str, auto_adjust=True)
    df_1h = ticker.history(interval="1h", start=start_str, end=end_str, auto_adjust=True)

    bars_1m = _yf_to_bars(df_1m, "1m", symbol)
    bars_5m = _yf_to_bars(df_5m, "5m", symbol)
    bars_1h = _yf_to_bars(df_1h, "1h", symbol)

    if not bars_1m:
        resolved = _resolve_ticker(symbol)
        raise ValueError(
            f"No 1m bars returned for {symbol} (resolved to {resolved!r}, {start_str} → {end_str}). "
            "yfinance only provides 1m data for the last 7 days."
        )

    return bars_1m, bars_5m, bars_1h


def save_bars_to_csv(
    bars: list[Bar],
    path: str | Path,
) -> None:
    """Write a bar list to a CSV file readable by ``load_bars_from_csv``.

    Columns: timestamp (ISO-8601 UTC), open, high, low, close, volume
    Creates parent directories as needed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        for b in bars:
            writer.writerow({
                "timestamp": b.timestamp.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            })
