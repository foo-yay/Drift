"""Smoke tests for the Streamlit replay chart and table builders.

These tests do NOT import streamlit — they only exercise chart.py, which is
pure Python + Plotly and safe to call in any pytest environment.
"""
from __future__ import annotations

from datetime import datetime, timezone

import plotly.graph_objects as go
import pandas as pd
import pytest

from drift.models import Bar, SignalEvent
from drift.replay.chart import build_chart, events_to_df


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def _bar(ts: str, price: float = 100.0) -> Bar:
    return Bar(
        timestamp=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1000,
        timeframe="1m",
        symbol="MNQ",
    )


def _bars(n: int, base_ts: str = "2026-04-10T14:00:00+00:00") -> list[Bar]:
    """Generate n sequential 1-minute bars."""
    from datetime import timedelta
    base = datetime.fromisoformat(base_ts)
    return [_bar((base + timedelta(minutes=i)).isoformat()) for i in range(n)]


def _trade_event(ts: str = "2026-04-10T14:00:00+00:00") -> SignalEvent:
    return SignalEvent(
        event_time=datetime.fromisoformat(ts),
        symbol="MNQ",
        final_outcome="TRADE_PLAN_ISSUED",
        final_reason="LONG | pullback_continuation | confidence=74",
        trade_plan={
            "bias": "LONG",
            "setup_type": "pullback_continuation",
            "confidence": 74,
            "entry_min": 99.0,
            "entry_max": 101.0,
            "stop_loss": 95.0,
            "take_profit_1": 107.0,
            "take_profit_2": None,
            "reward_risk_ratio": 1.8,
            "max_hold_minutes": 20,
            "thesis": "Test thesis.",
        },
        replay_outcome={
            "outcome": "TP1_HIT",
            "bars_elapsed": 5,
            "minutes_elapsed": 5,
            "exit_price": 107.0,
            "pnl_points": 7.0,
        },
    )


def _blocked_event() -> SignalEvent:
    return SignalEvent(
        event_time=datetime.fromisoformat("2026-04-10T13:00:00+00:00"),
        symbol="MNQ",
        final_outcome="BLOCKED",
        final_reason="session gate",
    )


# ------------------------------------------------------------------
# build_chart
# ------------------------------------------------------------------

class TestBuildChart:
    def test_returns_figure(self):
        bars = _bars(5)
        fig = build_chart(bars, [], selected_idx=None)
        assert isinstance(fig, go.Figure)

    def test_candlestick_trace_present(self):
        bars = _bars(5)
        fig = build_chart(bars, [], selected_idx=None)
        assert any(isinstance(t, go.Candlestick) for t in fig.data)

    def test_signal_shapes_added(self):
        bars = _bars(30)
        events = [_trade_event("2026-04-10T14:02:00+00:00")]
        fig = build_chart(bars, events, selected_idx=None)
        # At minimum: entry zone rect + stop line + tp1 line = 3 shapes
        assert len(fig.layout.shapes) >= 3

    def test_no_crash_with_only_blocked_events(self):
        bars = _bars(5)
        fig = build_chart(bars, [_blocked_event()], selected_idx=None)
        assert isinstance(fig, go.Figure)
        assert len(fig.layout.shapes) == 0

    def test_selected_signal_highlighted(self):
        bars = _bars(30)
        events_sel   = [_trade_event()]
        fig_selected = build_chart(bars, events_sel, selected_idx=0)
        fig_unselect = build_chart(bars, events_sel, selected_idx=None)
        # The selected figure should have a higher-alpha fill string
        shape_sel   = fig_selected.layout.shapes[0]["fillcolor"]
        shape_unsel = fig_unselect.layout.shapes[0]["fillcolor"]
        assert shape_sel != shape_unsel


# ------------------------------------------------------------------
# events_to_df
# ------------------------------------------------------------------

class TestEventsTodf:
    def test_empty_events_returns_empty_df(self):
        df = events_to_df([])
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_blocked_events_excluded(self):
        df = events_to_df([_blocked_event()])
        assert df.empty

    def test_trade_event_produces_row(self):
        df = events_to_df([_trade_event()])
        assert len(df) == 1

    def test_expected_columns_present(self):
        df = events_to_df([_trade_event()])
        for col in ("Bias", "Setup", "Conf", "Outcome", "PnL (pts)"):
            assert col in df.columns

    def test_outcome_populated(self):
        df = events_to_df([_trade_event()])
        assert df.iloc[0]["Outcome"] == "TP1_HIT"
        assert df.iloc[0]["PnL (pts)"] == pytest.approx(7.0)
