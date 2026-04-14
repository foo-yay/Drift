"""Tests for OrderBlockFeatures and RejectionBlockFeatures."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from drift.features.order_blocks import OrderBlockFeatures
from drift.features.rejection_blocks import RejectionBlockFeatures
from drift.models import Bar


def _bar(open_: float, high: float, low: float, close: float, i: int = 0) -> Bar:
    return Bar(
        timestamp=datetime(2026, 4, 14, 10, i, tzinfo=timezone.utc),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
        timeframe="5m",
        symbol="MNQ",
    )


class TestOrderBlockFeatures:
    def test_returns_empty_on_insufficient_bars(self):
        comp = OrderBlockFeatures()
        result = comp.compute([])
        assert result == {"order_blocks": []}

    def test_detects_bullish_order_block(self):
        # bearish candle followed by strong bull candle
        bars = [
            _bar(100, 101, 97, 98, i=0),   # bearish
            _bar(98, 106, 97, 105, i=1),   # strong bull (BOS)
            _bar(105, 106, 103, 104, i=2),
        ]
        comp = OrderBlockFeatures(lookback=50)
        result = comp.compute(bars)
        blocks = result["order_blocks"]
        assert any(b["direction"] == "bullish" for b in blocks)

    def test_detects_bearish_order_block(self):
        # bullish candle followed by strong bear candle
        bars = [
            _bar(100, 104, 99, 103, i=0),  # bullish
            _bar(103, 104, 95, 96, i=1),   # strong bear (BOS)
            _bar(96, 97, 94, 95, i=2),
        ]
        comp = OrderBlockFeatures(lookback=50)
        result = comp.compute(bars)
        blocks = result["order_blocks"]
        assert any(b["direction"] == "bearish" for b in blocks)

    def test_respects_max_blocks(self):
        bars = []
        # Alternate bearish/bull pairs to generate many blocks
        for i in range(20):
            bars.append(_bar(100, 104, 99, 103, i=i * 2))
            bars.append(_bar(103, 104, 95, 96, i=i * 2 + 1))
        comp = OrderBlockFeatures(lookback=50, max_blocks=2)
        result = comp.compute(bars)
        assert len(result["order_blocks"]) <= 2

    def test_block_has_required_fields(self):
        bars = [
            _bar(100, 101, 97, 98, i=0),
            _bar(98, 107, 97, 106, i=1),
            _bar(106, 108, 104, 105, i=2),
        ]
        comp = OrderBlockFeatures()
        result = comp.compute(bars)
        for block in result["order_blocks"]:
            assert "direction" in block
            assert "top" in block
            assert "bottom" in block
            assert "formed_at" in block
            assert "is_fresh" in block


class TestRejectionBlockFeatures:
    def test_returns_empty_on_insufficient_bars(self):
        comp = RejectionBlockFeatures()
        assert comp.compute([]) == {"rejection_blocks": []}

    def test_detects_bearish_rejection(self):
        # large upper wick
        bars = [_bar(100, 115, 99, 101, i=0)]
        comp = RejectionBlockFeatures(lookback=30)
        result = comp.compute(bars)
        assert any(b["direction"] == "bearish_rejection" for b in result["rejection_blocks"])

    def test_detects_bullish_rejection(self):
        # large lower wick
        bars = [_bar(100, 101, 85, 99, i=0)]
        comp = RejectionBlockFeatures(lookback=30)
        result = comp.compute(bars)
        assert any(b["direction"] == "bullish_rejection" for b in result["rejection_blocks"])

    def test_ignores_tiny_bars(self):
        # range < _MIN_RANGE_POINTS
        bars = [_bar(100.0, 100.5, 99.8, 100.2, i=0)]
        comp = RejectionBlockFeatures()
        result = comp.compute(bars)
        assert result["rejection_blocks"] == []

    def test_block_has_required_fields(self):
        bars = [_bar(100, 115, 99, 101, i=0)]
        comp = RejectionBlockFeatures()
        result = comp.compute(bars)
        for block in result["rejection_blocks"]:
            assert "direction" in block
            assert "level" in block
            assert "strength_pct" in block
            assert "formed_at" in block
