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
        assert config.instrument.symbol == "MNQ"
