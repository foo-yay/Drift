from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from drift.models import Bar


def bars_to_df(bars: list[Bar]) -> pd.DataFrame:
    """Convert a list of Bar objects into a clean DataFrame indexed by timestamp.

    Columns: open, high, low, close, volume (all float64).
    Rows are sorted oldest-first so that indicator calculations are correct.
    """
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    records = [
        {
            "timestamp": b.timestamp,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    df = pd.DataFrame(records).set_index("timestamp").sort_index()
    return df.astype(
        {"open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "float64"}
    )


class FeatureComputer(ABC):
    """Base class for all feature computation modules.

    Each subclass computes a specific group of indicators from a list of Bars
    and returns a plain dict of scalar values ready to be merged into the
    feature engine's working context.
    """

    @abstractmethod
    def compute(self, bars: list[Bar], **kwargs: object) -> dict[str, object]:
        """Compute indicator values for the given bars.

        Args:
            bars: OHLCV bars, ordered oldest-first.
            **kwargs: optional additional context passed by the engine.

        Returns:
            A flat dict of {field_name: value} pairs.
        """
