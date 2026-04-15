"""Replay chart and table builders for the Streamlit GUI.

Separated from streamlit_app.py so the chart logic can be unit-tested
without importing streamlit.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go

from drift.models import Bar, SignalEvent

_ET = ZoneInfo("America/New_York")

_OUTCOME_COLOR: dict[str, str] = {
    "TP1_HIT": "#00b386",
    "TP2_HIT": "#00e6b3",
    "STOP_HIT": "#e05252",
    "TIME_STOP": "#f0a500",
    "SESSION_END": "#888888",
}


def build_chart(
    bars_1m: list[Bar],
    events: list[SignalEvent],
    selected_idx: int | None = None,
) -> go.Figure:
    """Return a Plotly Figure with a candlestick chart and signal overlays.

    Args:
        bars_1m:      1-minute bars covering the replay period.
        events:       All SignalEvents produced by the replay engine.
        selected_idx: Index into the *trade-plan-only* subset of events that
                      is currently selected in the UI table.  That signal is
                      rendered at full opacity; others are dimmed.
    """
    ts_et = [b.timestamp.astimezone(_ET) for b in bars_1m]

    fig = go.Figure(
        go.Candlestick(
            x=ts_et,
            open=[b.open for b in bars_1m],
            high=[b.high for b in bars_1m],
            low=[b.low for b in bars_1m],
            close=[b.close for b in bars_1m],
            name="MNQ 1m",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            showlegend=False,
        )
    )

    trade_events = [
        e for e in events if e.final_outcome == "TRADE_PLAN_ISSUED" and e.trade_plan
    ]

    shapes: list[dict] = []
    # Scatter data for outcome marker triangles
    sc_x: list = []
    sc_y: list = []
    sc_colors: list = []
    sc_symbols: list = []
    sc_texts: list = []
    sc_sizes: list = []

    for i, event in enumerate(trade_events):
        tp = event.trade_plan or {}
        out = event.replay_outcome or {}
        sig_ts = event.event_time.astimezone(_ET)
        is_selected = i == selected_idx

        # Find the bar at or just after signal time to anchor the right edge
        zone_end_ts = sig_ts
        for j, b in enumerate(bars_1m):
            if b.timestamp.astimezone(_ET) >= sig_ts:
                end_j = min(j + 25, len(bars_1m) - 1)
                zone_end_ts = bars_1m[end_j].timestamp.astimezone(_ET)
                break

        bias = tp.get("bias", "LONG")
        alpha = "0.45" if is_selected else "0.18"
        border_alpha = "0.8" if is_selected else "0.4"

        if bias == "LONG":
            fill = f"rgba(38,166,154,{alpha})"
            border = f"rgba(38,166,154,{border_alpha})"
        else:
            fill = f"rgba(239,83,80,{alpha})"
            border = f"rgba(239,83,80,{border_alpha})"

        # Entry zone band
        shapes.append(dict(
            type="rect",
            x0=sig_ts, x1=zone_end_ts,
            y0=tp.get("entry_min"), y1=tp.get("entry_max"),
            xref="x", yref="y",
            fillcolor=fill,
            line=dict(width=0.8, color=border),
        ))
        # Stop horizontal line
        shapes.append(dict(
            type="line",
            x0=sig_ts, x1=zone_end_ts,
            y0=tp.get("stop_loss"), y1=tp.get("stop_loss"),
            xref="x", yref="y",
            line=dict(color="#e05252", width=1, dash="dot"),
        ))
        # TP1 horizontal line
        shapes.append(dict(
            type="line",
            x0=sig_ts, x1=zone_end_ts,
            y0=tp.get("take_profit_1"), y1=tp.get("take_profit_1"),
            xref="x", yref="y",
            line=dict(color="#26a69a", width=1, dash="dot"),
        ))
        # TP2 horizontal line (if present)
        if tp.get("take_profit_2") is not None:
            shapes.append(dict(
                type="line",
                x0=sig_ts, x1=zone_end_ts,
                y0=tp["take_profit_2"], y1=tp["take_profit_2"],
                xref="x", yref="y",
                line=dict(color="#00e6b3", width=1, dash="dot"),
            ))

        # Outcome marker triangle
        outcome_label = out.get("outcome", "")
        marker_color = _OUTCOME_COLOR.get(outcome_label, "#4488ff")
        anchor_y = tp.get("entry_max") if bias == "LONG" else tp.get("entry_min")
        sc_x.append(sig_ts)
        sc_y.append(anchor_y)
        sc_colors.append(marker_color)
        sc_symbols.append("triangle-up" if bias == "LONG" else "triangle-down")
        sc_texts.append(outcome_label or "PENDING")
        sc_sizes.append(14 if is_selected else 10)

    if sc_x:
        fig.add_trace(go.Scatter(
            x=sc_x,
            y=sc_y,
            mode="markers+text",
            marker=dict(
                symbol=sc_symbols,
                color=sc_colors,
                size=sc_sizes,
                line=dict(width=1, color="#111111"),
            ),
            text=sc_texts,
            textposition=[
                "top center" if s == "triangle-up" else "bottom center" for s in sc_symbols
            ],
            textfont=dict(size=9, color="#cccccc"),
            hoverinfo="text",
            showlegend=False,
            name="Signals",
        ))

    fig.update_layout(
        shapes=shapes,
        xaxis_rangeslider_visible=False,
        height=520,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="#111111",
        plot_bgcolor="#111111",
        font=dict(color="#cccccc", size=11),
        xaxis=dict(gridcolor="#1e1e1e", type="date"),
        yaxis=dict(gridcolor="#1e1e1e"),
    )
    return fig


def events_to_df(events: list[SignalEvent]) -> pd.DataFrame:
    """Convert TRADE_PLAN_ISSUED events to a display-ready DataFrame.

    All events with a trade plan are included.  Unresolved signals show
    ``Outcome="PENDING"`` and blank PnL.  The ``Source`` column shows how
    the signal was generated (``live``, ``replay``, or ``dry_run``).
    """
    rows = []
    for e in events:
        if e.final_outcome != "TRADE_PLAN_ISSUED" or not e.trade_plan:
            continue
        tp = e.trade_plan
        out = e.replay_outcome or {}
        rows.append({
            "Time (ET)": e.event_time.astimezone(_ET).strftime("%Y-%m-%d %H:%M"),
            "Source": getattr(e, "source", "live"),
            "Bias": tp.get("bias"),
            "Setup": tp.get("setup_type"),
            "Conf": tp.get("confidence"),
            "Entry": f"{tp.get('entry_min', 0):.1f} \u2013 {tp.get('entry_max', 0):.1f}",
            "Stop": tp.get("stop_loss"),
            "TP1": tp.get("take_profit_1"),
            "R:R": tp.get("reward_risk_ratio"),
            "Outcome": out.get("outcome") or "PENDING",
            "PnL (pts)": float(out["pnl_points"]) if "pnl_points" in out else None,
            "Min": int(out["minutes_elapsed"]) if "minutes_elapsed" in out else None,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["PnL (pts)"] = pd.to_numeric(df["PnL (pts)"], errors="coerce").astype("float64")
    df["Min"] = pd.to_numeric(df["Min"], errors="coerce").astype("Int64")
    return df


def build_equity_chart(
    trade_events: list[SignalEvent],
) -> tuple[go.Figure, list[int]]:
    """Build a Plotly equity curve with clickable scatter markers.

    Each scatter point represents a resolved TRADE_PLAN_ISSUED event.
    Clicking a point returns ``customdata`` = the index of that event in
    *trade_events*, so the caller can open the right signal detail dialog.

    Args:
        trade_events: All TRADE_PLAN_ISSUED events (resolved and pending).

    Returns:
        ``(figure, resolved_indices)`` where ``resolved_indices[i]`` is the
        index into *trade_events* for the i-th plotted point.
    """
    resolved = [(i, e) for i, e in enumerate(trade_events) if e.replay_outcome]

    if not resolved:
        fig = go.Figure()
        fig.update_layout(
            height=280,
            paper_bgcolor="#111111",
            plot_bgcolor="#111111",
            font=dict(color="#888888", size=12),
            annotations=[dict(
                text="No resolved signals yet — run drift backfill-outcomes or a new replay",
                x=0.5, y=0.5, xref="paper", yref="paper",
                showarrow=False, font=dict(color="#666666", size=13),
            )],
            margin=dict(l=10, r=10, t=10, b=10),
        )
        return fig, []

    resolved_indices = [i for i, _ in resolved]
    times      = [e.event_time.astimezone(_ET) for _, e in resolved]
    pnl_values = [float((e.replay_outcome or {}).get("pnl_points", 0.0)) for _, e in resolved]

    running: list[float] = []
    total = 0.0
    for pnl in pnl_values:
        total += pnl
        running.append(round(total, 2))

    colors = [
        _OUTCOME_COLOR.get((e.replay_outcome or {}).get("outcome", ""), "#4488ff")
        for _, e in resolved
    ]
    hover_texts = [
        (
            f"{(e.replay_outcome or {}).get('outcome', '')}  "
            f"{'+' if pnl >= 0 else ''}{pnl:.1f} pts"
        )
        for pnl, (_, e) in zip(pnl_values, resolved)
    ]

    curve_color = "#00b386" if total >= 0 else "#e05252"
    fill_color  = "rgba(0,179,134,0.12)" if total >= 0 else "rgba(224,82,82,0.12)"

    fig = go.Figure()

    # Background equity line (non-interactive)
    fig.add_trace(go.Scatter(
        x=times,
        y=running,
        mode="lines",
        line=dict(color=curve_color, width=2),
        fill="tozeroy",
        fillcolor=fill_color,
        hoverinfo="skip",
        showlegend=False,
    ))

    # Clickable scatter markers — one per resolved signal
    fig.add_trace(go.Scatter(
        x=times,
        y=running,
        mode="markers",
        marker=dict(
            color=colors,
            size=11,
            line=dict(width=1.5, color="#111111"),
            symbol="circle",
        ),
        customdata=resolved_indices,
        text=hover_texts,
        hovertemplate=(
            "%{x|%Y-%m-%d %H:%M ET}<br>"
            "%{text}<br>"
            "Cumulative: <b>%{y:.1f} pts</b>"
            "<extra></extra>"
        ),
        showlegend=False,
        name="signals",
    ))

    fig.update_layout(
        height=280,
        title=(
            f"Equity Curve — {len(resolved)} resolved  |  "
            f"Total: {'+' if total >= 0 else ''}{total:.1f} pts  "
            f"(click a dot to view signal detail)"
        ),
        paper_bgcolor="#111111",
        plot_bgcolor="#111111",
        font=dict(color="#cccccc", size=11),
        xaxis=dict(gridcolor="#1e1e1e", type="date"),
        yaxis=dict(gridcolor="#1e1e1e", title="Cumulative PnL (pts)"),
        margin=dict(l=10, r=10, t=44, b=10),
        dragmode=False,
    )
    return fig, resolved_indices
