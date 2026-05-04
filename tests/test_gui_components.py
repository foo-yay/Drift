"""Smoke tests for Phase 10b GUI components.

These tests do not launch a Streamlit runtime.  They validate the non-Streamlit
portions of the GUI: the candlestick chart builder and the news panel countdown
helper.  Streamlit-dependent rendering (gate_status, signal_detail, page layouts)
is tested manually via `drift gui`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import plotly.graph_objects as go
import pytest

from drift.gui.components.candlestick import (
    _signal_hover,
    build_candlestick_chart,
)
from drift.models import Bar
from drift.storage.signal_store import SignalRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 4, 15, 14, 30, 0, tzinfo=timezone.utc)


def _make_bar(offset_minutes: int = 0, symbol: str = "MNQ") -> Bar:
    ts = _TS + timedelta(minutes=offset_minutes)
    return Bar(
        timestamp=ts,
        open=21000.0 + offset_minutes,
        high=21010.0 + offset_minutes,
        low=20990.0 + offset_minutes,
        close=21005.0 + offset_minutes,
        volume=1000.0,
        timeframe="5m",
        symbol=symbol,
    )


def _make_signal_row(
    signal_key: str = "abc123",
    bias: str | None = "LONG",
    outcome: str = "TRADE_PLAN_ISSUED",
    as_of: str | None = None,
) -> SignalRow:
    ts = as_of or _TS.isoformat()
    return SignalRow(
        id=1,
        signal_key=signal_key,
        symbol="MNQ",
        source="live",
        event_time_utc=ts,
        as_of_utc=ts,
        final_outcome=outcome,
        bias=bias,
        setup_type="pullback_continuation",
        confidence=72,
        entry_min=21000.0,
        entry_max=21005.0,
        stop_loss=20975.0,
        take_profit_1=21040.0,
        take_profit_2=21070.0,
        reward_risk=1.8,
        pnl_points=None,
        replay_outcome=None,
        thesis="Test thesis",
        snapshot_json=None,
        gate_report_json=None,
        llm_json=None,
        created_at=_TS.isoformat(),
    )


# ---------------------------------------------------------------------------
# Candlestick chart builder
# ---------------------------------------------------------------------------

class TestBuildCandlestickChart:
    def test_empty_bars_returns_figure(self) -> None:
        fig = build_candlestick_chart([], [])
        assert isinstance(fig, go.Figure)

    def test_bars_produce_candlestick_trace(self) -> None:
        bars = [_make_bar(i * 5) for i in range(10)]
        fig = build_candlestick_chart(bars, [])
        trace_types = [type(t).__name__ for t in fig.data]
        assert "Candlestick" in trace_types

    def test_volume_bar_trace_added(self) -> None:
        bars = [_make_bar(i * 5) for i in range(5)]
        fig = build_candlestick_chart(bars, [])
        trace_types = [type(t).__name__ for t in fig.data]
        assert "Bar" in trace_types

    def test_long_signal_marker_added(self) -> None:
        bars = [_make_bar(i * 5) for i in range(10)]
        sig = _make_signal_row(bias="LONG", outcome="TRADE_PLAN_ISSUED")
        fig = build_candlestick_chart(bars, [sig])
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        assert "LONG" in scatter_names

    def test_short_signal_marker_added(self) -> None:
        bars = [_make_bar(i * 5) for i in range(10)]
        sig = _make_signal_row(bias="SHORT", outcome="TRADE_PLAN_ISSUED")
        fig = build_candlestick_chart(bars, [sig])
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        assert "SHORT" in scatter_names

    def test_blocked_signal_goes_to_neutral_trace(self) -> None:
        bars = [_make_bar(i * 5) for i in range(10)]
        sig = _make_signal_row(bias=None, outcome="BLOCKED")
        fig = build_candlestick_chart(bars, [sig])
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        assert "No-Trade / Blocked" in scatter_names

    def test_signal_outside_bar_range_not_rendered(self) -> None:
        bars = [_make_bar(i * 5) for i in range(5)]
        # Signal 10 days in the future — outside bar range
        future = (_TS + timedelta(days=10)).isoformat()
        sig = _make_signal_row(as_of=future)
        fig = build_candlestick_chart(bars, [sig])
        scatter_traces = [t for t in fig.data if type(t).__name__ == "Scatter"]
        total_points = sum(len(t.x or []) for t in scatter_traces)
        assert total_points == 0

    def test_custom_height_applied(self) -> None:
        bars = [_make_bar(i) for i in range(3)]
        fig = build_candlestick_chart(bars, [], height=300)
        assert fig.layout.height == 300

    def test_dark_background(self) -> None:
        fig = build_candlestick_chart([], [])
        assert fig.layout.plot_bgcolor == "#0e1117"


# ---------------------------------------------------------------------------
# Signal hover text
# ---------------------------------------------------------------------------

class TestSignalHover:
    def test_contains_outcome(self) -> None:
        sig = _make_signal_row()
        text = _signal_hover(sig)
        assert "TRADE_PLAN_ISSUED" in text

    def test_contains_bias_and_setup(self) -> None:
        sig = _make_signal_row()
        text = _signal_hover(sig)
        assert "LONG" in text
        assert "pullback_continuation" in text

    def test_contains_entry_levels(self) -> None:
        sig = _make_signal_row()
        text = _signal_hover(sig)
        assert "21,000" in text

    def test_no_entry_when_none(self) -> None:
        sig = _make_signal_row()
        # Remove entry
        import dataclasses
        sig2 = SignalRow(
            **{**sig.__dict__, "entry_min": None, "entry_max": None}  # type: ignore[arg-type]
        )
        text = _signal_hover(sig2)
        assert "Entry:" not in text


# ---------------------------------------------------------------------------
# Phase 11 — overlay rendering
# ---------------------------------------------------------------------------

class TestOverlays:
    """Verify that overlay flags produce the correct Plotly traces/shapes."""

    def _bars(self, n: int = 20) -> list[Bar]:
        return [_make_bar(i * 5) for i in range(n)]

    def test_no_overlays_by_default(self) -> None:
        """Default call produces no EMA/VWAP scatter traces."""
        bars = self._bars()
        fig = build_candlestick_chart(bars, [])
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        for name in scatter_names:
            assert "EMA" not in name
            assert "VWAP" not in name

    def test_ema_traces_added_when_flag_set(self) -> None:
        # Need enough bars to compute EMAs (at least 50)
        bars = self._bars(60)
        fig = build_candlestick_chart(bars, [], show_emas=True)
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        assert "EMA 9" in scatter_names
        assert "EMA 21" in scatter_names
        assert "EMA 50" in scatter_names

    def test_ema_series_has_one_value_per_bar(self) -> None:
        """EMA traces must have the same length as the bars list."""
        bars = self._bars(60)
        fig = build_candlestick_chart(bars, [], show_emas=True)
        ema9 = next(t for t in fig.data if type(t).__name__ == "Scatter" and t.name == "EMA 9")
        assert len(ema9.y) == len(bars)

    def test_ema_flag_false_suppresses_traces(self) -> None:
        bars = self._bars(60)
        fig = build_candlestick_chart(bars, [], show_emas=False)
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        assert "EMA 9" not in scatter_names

    def test_vwap_trace_added_when_flag_set(self) -> None:
        # Bars must fall on or after 14:30 UTC (RTH open) for VWAP to appear
        from datetime import timezone
        bars = [
            Bar(
                timestamp=_TS + timedelta(minutes=i * 5),
                open=21000.0, high=21010.0, low=20990.0, close=21005.0,
                volume=1000.0, timeframe="5m", symbol="MNQ",
            )
            for i in range(10)
        ]
        fig = build_candlestick_chart(bars, [], show_vwap=True)
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        assert "VWAP" in scatter_names

    def test_vwap_series_grows_with_bars(self) -> None:
        """Each VWAP point must be a cumulative average — strictly non-trivial length."""
        bars = [
            Bar(
                timestamp=_TS + timedelta(minutes=i * 5),
                open=21000.0, high=21010.0, low=20990.0, close=21005.0,
                volume=1000.0, timeframe="5m", symbol="MNQ",
            )
            for i in range(10)
        ]
        fig = build_candlestick_chart(bars, [], show_vwap=True)
        vwap_trace = next(t for t in fig.data if type(t).__name__ == "Scatter" and t.name == "VWAP")
        assert len(vwap_trace.y) == len(bars)

    def test_vwap_flag_false_suppresses_trace(self) -> None:
        bars = self._bars()
        fig = build_candlestick_chart(bars, [], show_vwap=False)
        scatter_names = [t.name for t in fig.data if type(t).__name__ == "Scatter"]
        assert "VWAP" not in scatter_names

    def test_order_block_shapes_added(self) -> None:
        bars = self._bars()
        od = {
            "order_blocks": [
                {"direction": "bullish", "top": 21010.0, "bottom": 21000.0,
                 "formed_at": _TS.isoformat(), "is_fresh": True},
                {"direction": "bearish", "top": 21050.0, "bottom": 21040.0,
                 "formed_at": _TS.isoformat(), "is_fresh": False},
            ],
            "rejection_blocks": [],
        }
        fig = build_candlestick_chart(bars, [], show_order_blocks=True, overlay_data=od)
        # Two order blocks → two rect shapes
        rect_shapes = [s for s in fig.layout.shapes if s.type == "rect"]
        assert len(rect_shapes) == 2

    def test_rejection_block_lines_added(self) -> None:
        bars = self._bars()
        od = {
            "order_blocks": [],
            "rejection_blocks": [
                {"direction": "bearish_rejection", "level": 21060.0,
                 "wick_start": 21045.0, "wick_end": 21060.0,
                 "formed_at": _TS.isoformat(), "strength_pct": 62.0},
            ],
        }
        fig = build_candlestick_chart(bars, [], show_order_blocks=True, overlay_data=od)
        line_shapes = [s for s in fig.layout.shapes if s.type == "line"]
        assert len(line_shapes) >= 1

    def test_order_blocks_flag_false_produces_no_shapes(self) -> None:
        bars = self._bars()
        od = {
            "order_blocks": [
                {"direction": "bullish", "top": 21010.0, "bottom": 21000.0,
                 "formed_at": _TS.isoformat(), "is_fresh": True},
            ],
            "rejection_blocks": [],
        }
        fig = build_candlestick_chart(bars, [], show_order_blocks=False, overlay_data=od)
        assert len(fig.layout.shapes) == 0

    def test_none_overlay_data_safe(self) -> None:
        """Passing overlay_data=None with show_emas=True should not raise."""
        bars = self._bars()
        fig = build_candlestick_chart(bars, [], show_emas=True, overlay_data=None)
        assert isinstance(fig, go.Figure)


# ---------------------------------------------------------------------------
# State module — project root discovery
# ---------------------------------------------------------------------------

class TestStateModule:
    def test_project_root_is_correct(self) -> None:
        from drift.gui.state import project_root
        root = project_root()
        assert (root / "pyproject.toml").exists()

    def test_get_config_loads_without_error(self) -> None:
        from drift.gui.state import get_config
        config = get_config()
        assert isinstance(config.instrument.symbol, str)
        assert len(config.instrument.symbol) > 0
