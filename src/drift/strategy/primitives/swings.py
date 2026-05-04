"""Swing high / low detection — deterministic, pure-pandas.

Definition
----------
A **swing high** at index i is a local high where the ``high`` of bar i is
strictly greater than the ``high`` of every bar within ``lookback`` bars on
both sides.  Ties on either side disqualify the candidate.

A **swing low** at index i is the symmetric definition using ``low``.

This is intentionally simple and testable.  It does NOT use adaptive
methods or peak-detection libraries, so the output is fully reproducible
from the same bar sequence.

Notes
-----
- Bars must be supplied oldest-first (standard throughout the project).
- The most recent ``lookback`` bars cannot be a confirmed swing because
  there are not enough subsequent bars to verify the right-side condition.
  This is correct — we should not call an unconfirmed extremum a swing.
"""
from __future__ import annotations

from dataclasses import dataclass

from drift.models import Bar


@dataclass(frozen=True)
class SwingPoint:
    """A confirmed swing high or swing low."""

    bar_index: int          # index into the bars list
    price: float            # the relevant extreme (high for SH, low for SL)
    kind: str               # "high" | "low"
    timestamp: object       # bar.timestamp for reference


def find_swing_highs(bars: list[Bar], lookback: int = 3) -> list[SwingPoint]:
    """Return all confirmed swing highs in *bars*, oldest first.

    A swing high at index ``i`` requires:
        bars[i].high > bars[j].high  for all j in [i-lookback, i-1]  (left side)
        bars[i].high > bars[j].high  for all j in [i+1, i+lookback]  (right side)

    Args:
        bars:     OHLCV bars, oldest-first.
        lookback: Number of bars on each side to check.  Must be >= 1.

    Returns:
        List of SwingPoint objects, oldest-first.  Empty list if fewer bars
        than 2*lookback+1 are supplied.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    result: list[SwingPoint] = []
    n = len(bars)
    for i in range(lookback, n - lookback):
        candidate = bars[i].high
        left_ok = all(candidate > bars[j].high for j in range(i - lookback, i))
        right_ok = all(candidate > bars[j].high for j in range(i + 1, i + lookback + 1))
        if left_ok and right_ok:
            result.append(SwingPoint(
                bar_index=i,
                price=candidate,
                kind="high",
                timestamp=bars[i].timestamp,
            ))
    return result


def find_swing_lows(bars: list[Bar], lookback: int = 3) -> list[SwingPoint]:
    """Return all confirmed swing lows in *bars*, oldest first.

    A swing low at index ``i`` requires:
        bars[i].low < bars[j].low  for all j in [i-lookback, i-1]
        bars[i].low < bars[j].low  for all j in [i+1, i+lookback]

    Args:
        bars:     OHLCV bars, oldest-first.
        lookback: Number of bars on each side to check.  Must be >= 1.

    Returns:
        List of SwingPoint objects, oldest-first.
    """
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    result: list[SwingPoint] = []
    n = len(bars)
    for i in range(lookback, n - lookback):
        candidate = bars[i].low
        left_ok = all(candidate < bars[j].low for j in range(i - lookback, i))
        right_ok = all(candidate < bars[j].low for j in range(i + 1, i + lookback + 1))
        if left_ok and right_ok:
            result.append(SwingPoint(
                bar_index=i,
                price=candidate,
                kind="low",
                timestamp=bars[i].timestamp,
            ))
    return result
