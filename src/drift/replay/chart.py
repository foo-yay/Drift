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
    annotations: list[dict] = []

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

        # Outcome annotation at signal time
        outcome_label = out.get("outcome", "")
        marker_color = _OUTCOME_COLOR.get(outcome_label, "#999")
        arrow = "▲" if bias == "LONG" else "▽"
        anchor_y = tp.get("entry_max") if bias == "LONG" else tp.get("entry_min")
        annotations.append(dict(
            x=sig_ts,
            y=anchor_y,
            xref="x",
            yref="y",
            text=f"<b>{arrow}</b> {outcome_label}",
            font=dict(size=9, color=marker_color),
            showarrow=False,
            yanchor="bottom" if bias == "LONG" else "top",
        ))

    fig.update_layout(
        shapes=shapes,
        annotations=annotations,
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
    """Convert TRADE_PLAN_ISSUED events to a display-ready DataFrame."""
    rows = []
    for e in events:
        if e.final_outcome != "TRADE_PLAN_ISSUED" or not e.trade_plan:
            continue
        tp = e.trade_plan
        out = e.replay_outcome or {}
        rows.append({
            "Time (ET)": e.event_time.astimezone(_ET).strftime("%Y-%m-%d %H:%M"),
            "Bias": tp.get("bias"),
            "Setup": tp.get("setup_type"),
            "Conf": tp.get("confidence"),
            "Entry": f"{tp.get('entry_min', 0):.1f} \u2013 {tp.get('entry_max', 0):.1f}",
            "Stop": tp.get("stop_loss"),
            "TP1": tp.get("take_profit_1"),
            "R:R": tp.get("reward_risk_ratio"),
            "Outcome": out.get("outcome") or "",
            "PnL (pts)": float(out["pnl_points"]) if "pnl_points" in out else None,
            "Min": int(out["minutes_elapsed"]) if "minutes_elapsed" in out else None,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["PnL (pts)"] = pd.to_numeric(df["PnL (pts)"], errors="coerce").astype("float64")
    df["Min"] = pd.to_numeric(df["Min"], errors="coerce").astype("Int64")
    return df
