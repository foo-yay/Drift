"""Plotly candlestick chart builder — no Streamlit imports here.

Keeping Streamlit out of this module allows the chart logic to be unit-tested
without launching a Streamlit runtime.
"""
from __future__ import annotations

from datetime import date, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import plotly.graph_objects as go

if TYPE_CHECKING:
    from drift.models import Bar
    from drift.storage.signal_store import SignalRow

_ET = ZoneInfo("America/New_York")


def _to_et(ts):
    """Return a timezone-aware datetime in US/Eastern for x-axis display."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(_ET)

# Marker config per outcome category
_LONG_MARKER = dict(symbol="triangle-up", size=12, color="limegreen",
                    line=dict(width=1, color="darkgreen"))
_SHORT_MARKER = dict(symbol="triangle-down", size=12, color="tomato",
                     line=dict(width=1, color="darkred"))
_NEUTRAL_MARKER = dict(symbol="diamond", size=9, color="gray",
                       line=dict(width=1, color="#555"))


def _signal_marker_props(bias: str | None, outcome: str) -> dict:
    if outcome != "TRADE_PLAN_ISSUED":
        return _NEUTRAL_MARKER
    if (bias or "").upper() == "LONG":
        return _LONG_MARKER
    if (bias or "").upper() == "SHORT":
        return _SHORT_MARKER
    return _NEUTRAL_MARKER


def build_candlestick_chart(
    bars: "list[Bar]",
    signals: "list[SignalRow]",
    timeframe: str = "5m",
    height: int = 500,
) -> go.Figure:
    """Build a Plotly candlestick chart with signal overlays.

    Args:
        bars: List of ``Bar`` objects from the data provider. May be empty.
        signals: ``SignalRow`` records from SQLite (last 7 days).  Markers are
                 only rendered if their ``as_of_utc`` timestamp falls within the
                 visible bar range.
        timeframe: Display label only — used for the chart title.
        height: Chart height in pixels.

    Returns:
        A configured Plotly ``Figure`` ready for ``st.plotly_chart()``.
    """
    fig = go.Figure()

    if bars:
        ts   = [_to_et(b.timestamp) for b in bars]
        o    = [b.open for b in bars]
        h    = [b.high for b in bars]
        lo   = [b.low for b in bars]
        c    = [b.close for b in bars]

        fig.add_trace(go.Candlestick(
            x=ts, open=o, high=h, low=lo, close=c,
            name=bars[0].symbol if bars else "Price",
            increasing_line_color="limegreen",
            decreasing_line_color="tomato",
        ))

        # Volume sub-trace (secondary y)
        vol_colors = [
            "rgba(50,205,50,0.4)" if bars[i].close >= bars[i].open else "rgba(255,99,71,0.4)"
            for i in range(len(bars))
        ]
        fig.add_trace(go.Bar(
            x=ts,
            y=[b.volume for b in bars],
            name="Volume",
            marker_color=vol_colors,
            yaxis="y2",
            showlegend=False,
        ))

        bar_ts_set = {b.timestamp for b in bars}
        bar_low  = {b.timestamp: b.low  for b in bars}
        bar_high = {b.timestamp: b.high for b in bars}

    # -- Signal markers -------------------------------------------------
    if signals and bars:
        bar_times_utc = {b.timestamp.replace(tzinfo=timezone.utc) if b.timestamp.tzinfo is None else b.timestamp
                         for b in bars}
        min_bar = min(b.timestamp for b in bars)
        max_bar = max(b.timestamp for b in bars)

        # Group markers by category for a single trace each (legend dedup)
        groups: dict[str, list] = {"LONG": [], "SHORT": [], "OTHER": []}
        for sig in signals:
            if not sig.as_of_utc:
                continue
            try:
                from datetime import datetime
                sig_ts = datetime.fromisoformat(sig.as_of_utc)
                if sig_ts.tzinfo is None:
                    sig_ts = sig_ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if sig_ts < min_bar or sig_ts > max_bar:
                continue

            # Convert to ET so the marker aligns with the ET x-axis
            sig_ts_et = _to_et(sig_ts)

            # Find closest bar for y-positioning (compare in UTC)
            closest = min(bars, key=lambda b, s=sig_ts: abs((b.timestamp - s).total_seconds()))
            if (sig.bias or "").upper() == "LONG" and sig.final_outcome == "TRADE_PLAN_ISSUED":
                groups["LONG"].append((sig_ts_et, closest.low * 0.9995, sig))
            elif (sig.bias or "").upper() == "SHORT" and sig.final_outcome == "TRADE_PLAN_ISSUED":
                groups["SHORT"].append((sig_ts_et, closest.high * 1.0005, sig))
            else:
                mid = (closest.high + closest.low) / 2
                groups["OTHER"].append((sig_ts_et, mid, sig))

        _add_signal_trace(fig, groups["LONG"], "LONG", _LONG_MARKER)
        _add_signal_trace(fig, groups["SHORT"], "SHORT", _SHORT_MARKER)
        _add_signal_trace(fig, groups["OTHER"], "No-Trade / Blocked", _NEUTRAL_MARKER)

    # -- Layout ---------------------------------------------------------
    last_close = bars[-1].close if bars else None
    title = f"{bars[0].symbol if bars else '—'}   {timeframe}"
    if last_close:
        title += f"   {last_close:,.2f}"

    fig.update_layout(
        title=dict(text=title, font=dict(size=15)),
        height=height,
        xaxis_rangeslider_visible=False,
        xaxis=dict(
            type="date",
            showgrid=True,
            gridcolor="#2a2a2a",
            tickformat="%H:%M<br>%b %d",
            title="Time (ET)",
            rangeselector=dict(
                buttons=[
                    dict(count=1,  label="1H",  step="hour",  stepmode="backward"),
                    dict(count=4,  label="4H",  step="hour",  stepmode="backward"),
                    dict(count=1,  label="1D",  step="day",   stepmode="backward"),
                    dict(count=7,  label="1W",  step="day",   stepmode="backward"),
                    dict(count=1,  label="1M",  step="month", stepmode="backward"),
                    dict(step="all", label="All"),
                ],
                activecolor="#1f77b4",
                bgcolor="#1a1a2e",
                bordercolor="#333",
                font=dict(color="#fafafa", size=11),
                x=0,
                y=1.02,
            ),
        ),
        yaxis=dict(
            title="Price",
            side="right",
            showgrid=True,
            gridcolor="#2a2a2a",
            domain=[0.25, 1.0],
        ),
        yaxis2=dict(
            title="Volume",
            side="left",
            showgrid=False,
            domain=[0.0, 0.20],
        ),
        plot_bgcolor="#0e1117",
        paper_bgcolor="#0e1117",
        font=dict(color="#fafafa"),
        legend=dict(orientation="h", y=1.12, x=0),
        margin=dict(l=20, r=60, t=80, b=40),
    )
    return fig


def _add_signal_trace(
    fig: go.Figure,
    entries: list[tuple],
    name: str,
    marker: dict,
) -> None:
    if not entries:
        return
    xs = [e[0] for e in entries]
    ys = [e[1] for e in entries]
    texts = [_signal_hover(e[2]) for e in entries]
    sig_keys = [e[2].signal_key for e in entries]

    fig.add_trace(go.Scatter(
        x=xs,
        y=ys,
        mode="markers",
        name=name,
        marker=marker,
        hovertext=texts,
        hoverinfo="text",
        customdata=sig_keys,
    ))


def _signal_hover(sig: "SignalRow") -> str:
    from datetime import datetime
    try:
        ts = datetime.fromisoformat(sig.as_of_utc or sig.event_time_utc)
        ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        ts_str = sig.as_of_utc or "?"
    lines = [
        f"<b>{sig.final_outcome}</b>",
        f"{ts_str}",
        f"Bias: {sig.bias or '—'}  Setup: {sig.setup_type or '—'}",
        f"Confidence: {sig.confidence or '—'}",
    ]
    if sig.entry_min:
        lines.append(f"Entry: {sig.entry_min:,.2f}–{sig.entry_max:,.2f}")
    if sig.stop_loss:
        lines.append(f"Stop: {sig.stop_loss:,.2f}  TP1: {sig.take_profit_1:,.2f}")
    return "<br>".join(lines)
