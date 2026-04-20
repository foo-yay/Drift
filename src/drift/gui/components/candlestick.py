"""Plotly candlestick chart builder — no Streamlit imports here.

Keeping Streamlit out of this module allows the chart logic to be unit-tested
without launching a Streamlit runtime.

Overlay support (Phase 11)
--------------------------
``build_candlestick_chart`` accepts three optional overlay flags and an
``overlay_data`` dict that carries pre-computed values from the most recent
``MarketSnapshot`` stored in SQLite.  The dict shape (all keys optional):

    {
        # EMA values — keyed by period (int)
        "ema_9":  float | None,
        "ema_21": float | None,
        "ema_50": float | None,
        # VWAP
        "vwap":   float | None,
        # Order blocks — list[dict] from OrderBlockFeatures
        "order_blocks":     [...],
        # Rejection blocks — list[dict] from RejectionBlockFeatures
        "rejection_blocks": [...],
    }

When a key is missing or None the corresponding overlay is silently skipped.
"""
from __future__ import annotations

from datetime import date, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import plotly.graph_objects as go

if TYPE_CHECKING:
    from drift.models import Bar
    from drift.storage.signal_store import SignalRow

_ET = ZoneInfo("America/New_York")

# EMA palette — consistent colours across timeframes
_EMA_COLOURS = {9: "#f5a623", 21: "#50e3c2", 50: "#9b59b6"}
_VWAP_COLOUR = "#4fc3f7"   # light-blue dashed line
_OB_BULL_FILL = "rgba(80, 200, 120, 0.12)"
_OB_BULL_LINE = "rgba(80, 200, 120, 0.55)"
_OB_BEAR_FILL = "rgba(230, 80,  80,  0.12)"
_OB_BEAR_LINE = "rgba(230, 80,  80,  0.55)"
_REJ_BULL_LINE = "rgba(80,  200, 120, 0.70)"
_REJ_BEAR_LINE = "rgba(230, 80,  80,  0.70)"


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
    show_emas: bool = False,
    show_vwap: bool = False,
    show_order_blocks: bool = False,
    overlay_data: "dict[str, Any] | None" = None,
    live_price: float | None = None,
    watch_levels: "list[dict] | None" = None,
    active_trade_plan: "dict | None" = None,
) -> go.Figure:
    """Build a Plotly candlestick chart with optional overlays.

    Args:
        bars: List of ``Bar`` objects from the data provider. May be empty.
        signals: ``SignalRow`` records from SQLite.  Markers are only rendered
                 if their ``as_of_utc`` timestamp falls within the visible range.
        timeframe: Display label used for the chart title.
        height: Chart height in pixels.
        show_emas: When True, draw EMA 9 / 21 / 50 lines from ``overlay_data``.
        show_vwap: When True, draw a VWAP dashed line from ``overlay_data``.
        show_order_blocks: When True, draw order-block and rejection-block zones
                           from ``overlay_data``.
        overlay_data: Dict of pre-computed overlay values (see module docstring).
                      Safe to omit or pass ``None`` — missing keys are ignored.
        live_price: When provided, draws a dotted horizontal line at this price
                    level labelled "Live".  Use ``fast_info.last_price`` from
                    yfinance — it is near-real-time (~seconds stale).
        watch_levels: Active watch conditions to overlay as horizontal lines.
                      Each dict must have ``condition_type`` and ``value`` keys.
                      Only ``price_above`` / ``price_below`` types are drawn
                      (RSI watches have no meaningful price-axis representation).
        active_trade_plan: When provided, draws horizontal lines for the pending
                      trade plan's entry zone, stop loss, TP1, and TP2.  Pass
                      the most recent unresolved SignalRow's trade plan fields as
                      a dict with keys: bias, entry_min, entry_max, stop_loss,
                      take_profit_1, take_profit_2.  Cleared automatically when
                      the signal is resolved (caller passes None).

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

    # -- Overlays -------------------------------------------------------
    # EMA and VWAP series are computed from the bars list directly so that
    # the lines follow price action across the chart, not flat horizontals.
    od = overlay_data or {}

    if show_emas and bars:
        import pandas as pd
        closes_s = pd.Series([b.close for b in bars], dtype=float)
        ts_et = [_to_et(b.timestamp) for b in bars]
        for period, colour in _EMA_COLOURS.items():
            if len(closes_s) >= period:
                ema_vals = closes_s.ewm(span=period, adjust=False).mean().tolist()
                fig.add_trace(go.Scatter(
                    x=ts_et,
                    y=ema_vals,
                    mode="lines",
                    name=f"EMA {period}",
                    line=dict(color=colour, width=1.5),
                    hovertemplate=f"EMA {period}: %{{y:,.2f}}<extra></extra>",
                ))

    if show_vwap and bars:
        import datetime as _dt
        # Anchor the RTH session open to the bars' own date (not today's
        # wall-clock date) so the chart works correctly for replay/historical
        # bars as well as live bars.
        _last_ts = bars[-1].timestamp if bars[-1].timestamp.tzinfo else bars[-1].timestamp.replace(tzinfo=timezone.utc)
        _bar_date = _last_ts.astimezone(timezone.utc).date()
        rth_open_utc = _dt.datetime(
            _bar_date.year, _bar_date.month, _bar_date.day,
            14, 30, tzinfo=timezone.utc,  # 09:30 ET = 14:30 UTC
        )
        vwap_ts: list = []
        vwap_vals: list = []
        cum_tp_vol = 0.0
        cum_vol = 0.0
        for b in bars:
            bar_ts = b.timestamp if b.timestamp.tzinfo else b.timestamp.replace(tzinfo=timezone.utc)
            if bar_ts < rth_open_utc:
                continue
            cum_tp_vol += (b.high + b.low + b.close) / 3 * b.volume
            cum_vol    += b.volume
            if cum_vol > 0:
                vwap_ts.append(_to_et(bar_ts))
                vwap_vals.append(cum_tp_vol / cum_vol)
        if vwap_ts:
            fig.add_trace(go.Scatter(
                x=vwap_ts,
                y=vwap_vals,
                mode="lines",
                name="VWAP",
                line=dict(color=_VWAP_COLOUR, width=2, dash="dash"),
                hovertemplate="VWAP: %{y:,.2f}<extra></extra>",
            ))

    if show_order_blocks and bars:
        x0 = _to_et(bars[0].timestamp)
        x1 = _to_et(bars[-1].timestamp)

        for ob in od.get("order_blocks", []):
            is_bull = (ob.get("direction") or "") == "bullish"
            fill = _OB_BULL_FILL if is_bull else _OB_BEAR_FILL
            line_c = _OB_BULL_LINE if is_bull else _OB_BEAR_LINE
            label = "OB ▲" if is_bull else "OB ▼"
            fresh_tag = " (fresh)" if ob.get("is_fresh") else " (mitigated)"
            top = float(ob["top"])
            bot = float(ob["bottom"])
            fig.add_shape(
                type="rect",
                x0=x0, x1=x1,
                y0=bot, y1=top,
                fillcolor=fill,
                line=dict(color=line_c, width=1, dash="dot"),
                layer="below",
            )
            fig.add_annotation(
                x=x1, y=(top + bot) / 2,
                text=f"{label}{fresh_tag} {top:,.1f}–{bot:,.1f}",
                showarrow=False,
                xanchor="right",
                font=dict(size=9, color=line_c),
            )

        for rb in od.get("rejection_blocks", []):
            is_bull = (rb.get("direction") or "") == "bullish_rejection"
            line_c = _REJ_BULL_LINE if is_bull else _REJ_BEAR_LINE
            label = "Rej ▲" if is_bull else "Rej ▼"
            level = float(rb["level"])
            fig.add_shape(
                type="line",
                x0=x0, x1=x1,
                y0=level, y1=level,
                line=dict(color=line_c, width=1, dash="dashdot"),
                layer="below",
            )
            fig.add_annotation(
                x=x1, y=level,
                text=f"{label} {level:,.1f} ({rb.get('strength_pct', '?')}%)",
                showarrow=False,
                xanchor="right",
                font=dict(size=9, color=line_c),
            )

    # -- Active trade plan levels ---------------------------------------
    if active_trade_plan and bars:
        tp_bias  = (active_trade_plan.get("bias") or "LONG").upper()
        _is_long = tp_bias == "LONG"
        _entry_color = "rgba(80, 200, 120, 0.80)"   # green
        _stop_color  = "rgba(230,  80,  80, 0.80)"  # red
        _tp1_color   = "rgba(100, 180, 255, 0.85)"  # blue
        _tp2_color   = "rgba(100, 180, 255, 0.50)"  # faded blue

        entry_min = active_trade_plan.get("entry_min")
        entry_max = active_trade_plan.get("entry_max")
        stop_loss = active_trade_plan.get("stop_loss")
        tp1       = active_trade_plan.get("take_profit_1")
        tp2       = active_trade_plan.get("take_profit_2")

        if entry_min and entry_max:
            # Shade the entry zone as a rectangle spanning the full x range
            fig.add_hrect(
                y0=entry_min, y1=entry_max,
                fillcolor="rgba(80, 200, 120, 0.07)",
                line_width=0,
                annotation_text=f"Entry Zone  {entry_min:,.2f}–{entry_max:,.2f}",
                annotation_position="top right",
                annotation_font=dict(size=10, color=_entry_color),
            )
            # Top and bottom edges of the zone
            for lvl in (entry_min, entry_max):
                fig.add_hline(y=lvl, line=dict(color=_entry_color, width=1, dash="dot"))
        if stop_loss:
            fig.add_hline(
                y=stop_loss,
                line=dict(color=_stop_color, width=1.5, dash="dash"),
                annotation_text=f"Stop  {stop_loss:,.2f}",
                annotation_position="bottom right" if _is_long else "top right",
                annotation_font=dict(size=10, color=_stop_color),
            )
        if tp1:
            fig.add_hline(
                y=tp1,
                line=dict(color=_tp1_color, width=1.5, dash="dash"),
                annotation_text=f"TP1  {tp1:,.2f}",
                annotation_position="top right" if _is_long else "bottom right",
                annotation_font=dict(size=10, color=_tp1_color),
            )
        if tp2:
            fig.add_hline(
                y=tp2,
                line=dict(color=_tp2_color, width=1, dash="dot"),
                annotation_text=f"TP2  {tp2:,.2f}",
                annotation_position="top right" if _is_long else "bottom right",
                annotation_font=dict(size=10, color=_tp2_color),
            )

    # -- Live price line ------------------------------------------------
    if live_price is not None and bars:
        fig.add_hline(
            y=live_price,
            line=dict(color="rgba(255, 255, 255, 0.50)", width=1, dash="dot"),
            annotation_text=f"Live  {live_price:,.2f}",
            annotation_position="bottom right",
            annotation_font=dict(size=10, color="rgba(255,255,255,0.65)"),
        )

    # -- Watch condition lines ------------------------------------------
    _WATCH_STYLE: dict[str, tuple[str, str, str]] = {
        "price_above": ("rgba(80, 200, 120, 0.85)", "▲", "top right"),
        "price_below": ("rgba(230,  80,  80, 0.85)", "▼", "bottom right"),
    }
    if watch_levels and bars:
        for w in watch_levels:
            ctype = w.get("condition_type", "")
            if ctype not in _WATCH_STYLE:
                continue
            value = w.get("value")
            if value is None:
                continue
            colour, arrow, pos = _WATCH_STYLE[ctype]
            fig.add_hline(
                y=value,
                line=dict(color=colour, width=1.5, dash="dash"),
                annotation_text=f"{arrow} Watch  {value:,.2f}",
                annotation_position=pos,
                annotation_font=dict(size=10, color=colour),
            )

    # -- Layout ---------------------------------------------------------
    title = f"{bars[0].symbol if bars else '—'}   {timeframe}"

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
        legend=dict(orientation="h", y=1.06, x=0),
        margin=dict(l=20, r=60, t=60, b=20),
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
