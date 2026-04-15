"""Live Monitor — Page 1 of the Drift GUI.

The primary screen: candlestick chart with signal markers, gate status panel,
and today's macro events.  The chart section auto-refreshes every 15 minutes
using ``@st.fragment(run_every=900)``.

Layout
------
┌──────────────────────────────────────┬─────────────────────┐
│  Header: price + session + last refresh                       │
├──────────────────────────────────────┤                       │
│  [tf selector]   [overlay stubs]     │  ENGINE STATUS        │
│                                      │  ● Gate results       │
│       Plotly Candlestick chart       │  ● Last trade plan    │
│       Signal markers overlaid        │                       │
├──────────────────────────────────────┤                       │
│  TODAY'S MACRO EVENTS (news_panel)   │                       │
└──────────────────────────────────────┴───────────────────────┘
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from drift.gui.components.candlestick import build_candlestick_chart
from drift.gui.components.gate_status import render_gate_status, render_last_trade_plan
from drift.gui.components.news_panel import render_news_panel
from drift.gui.components.signal_detail import show_signal_detail
from drift.gui.state import get_config, get_store

_ET = ZoneInfo("America/New_York")

# Timeframe → (yf interval, lookback bars)
_TF_CONFIG: dict[str, tuple[str, int]] = {
    "1m":  ("1m",  180),
    "5m":  ("5m",  120),
    "1H":  ("1h",   48),
}


def page() -> None:
    """Entry point called by ``gui/app.py``."""
    # ------------------------------------------------------------------
    # Config + store (cached for performance)
    # ------------------------------------------------------------------
    config = _load_config()
    store  = _open_store(config)

    symbol     = config.instrument.symbol
    filter_ctry = list(getattr(config.calendar, "filter_countries", ["USD"]))

    # ------------------------------------------------------------------
    # Timeframe selector (outside the fragment — controls full page rerun)
    # ------------------------------------------------------------------
    tf = st.radio(
        "Timeframe",
        list(_TF_CONFIG.keys()),
        index=1,          # default 5m
        horizontal=True,
        label_visibility="collapsed",
        key="live_tf",
    )

    # Overlay toggle stubs (Phase 10d will wire these up)
    with st.expander("Overlays (coming in Phase 10d)", expanded=False):
        col1, col2, col3 = st.columns(3)
        col1.checkbox("EMAs",         value=False, disabled=True)
        col2.checkbox("VWAP",         value=False, disabled=True)
        col3.checkbox("Order Blocks", value=False, disabled=True)

    # ------------------------------------------------------------------
    # Main layout: chart (left, 70%) + status panel (right, 30%)
    # ------------------------------------------------------------------
    chart_col, status_col = st.columns([0.70, 0.30], gap="large")

    with status_col:
        _render_status_panel(store)

    with chart_col:
        _chart_fragment(symbol, tf, store)

    # ------------------------------------------------------------------
    # Macro events strip (bottom)
    # ------------------------------------------------------------------
    st.divider()
    render_news_panel(filter_countries=filter_ctry)


# ---------------------------------------------------------------------------
# Chart fragment — auto-refreshes every 900 s
# ---------------------------------------------------------------------------

def _chart_fragment(symbol: str, tf: str, store) -> None:
    """Wrapped in @st.fragment so it auto-refreshes independently."""

    @st.fragment(run_every=900)
    def _inner() -> None:
        interval, lookback = _TF_CONFIG[tf]

        with st.spinner(f"Fetching {symbol} {tf} bars…"):
            bars = _fetch_bars(symbol, interval, lookback)

        # Signal markers: last 7 days from SQLite
        seven_days_ago = date.today() - timedelta(days=7)
        try:
            signals = store.query(date_start=seven_days_ago)
        except Exception:  # noqa: BLE001
            signals = []

        fig = build_candlestick_chart(bars, signals, timeframe=tf, height=500)

        # Offer click-to-detail via plotly selected point → session state
        selected = st.plotly_chart(
            fig,
            width="stretch",
            on_select="rerun",
            key=f"chart_{tf}",
        )

        # If user clicked a signal marker, open the detail dialog
        _handle_chart_click(selected, signals)

        # Timestamp of last fetch
        now_et = datetime.now(tz=_ET)
        st.caption(
            f"Last updated {now_et.strftime('%H:%M')} ET  •  "
            f"{len(bars)} bars  •  "
            f"{sum(1 for b in bars if True)} candles  •  "
            f"auto-refresh every 15 min"
        )

    _inner()


def _handle_chart_click(selected: dict | None, signals: list) -> None:
    """If a signal marker was clicked, open the detail dialog."""
    if not selected:
        return
    pts = (selected or {}).get("selection", {}).get("points", [])
    if not pts:
        return
    pt = pts[0]
    key = (pt.get("customdata") or [None])[0] if isinstance(pt.get("customdata"), list) else pt.get("customdata")
    if not key:
        return
    sig = next((s for s in signals if s.signal_key == key), None)
    if sig:
        show_signal_detail(sig)


# ---------------------------------------------------------------------------
# Status panel (right column)
# ---------------------------------------------------------------------------

def _render_status_panel(store) -> None:
    """Shows gate results + last trade plan from the most recent SQLite signal."""
    st.markdown("**Engine Status**")

    try:
        from drift.gui.state import project_root
        kill_path = project_root() / "data" / ".kill_switch"
        if kill_path.exists():
            st.error("🔴 KILL SWITCH ACTIVE", icon="🚨")
        else:
            st.success("● Running", icon="✅")
    except Exception:  # noqa: BLE001
        st.info("● Status unknown")

    st.divider()

    try:
        recent = store.query(limit=1, order_desc=True)
        last_sig = recent[0] if recent else None
    except Exception:  # noqa: BLE001
        last_sig = None

    render_gate_status(last_sig)
    render_last_trade_plan(last_sig)


# ---------------------------------------------------------------------------
# Cached helpers (avoid reloading every fragment run)
# ---------------------------------------------------------------------------

@st.cache_resource(ttl=3600)
def _load_config():
    return get_config()


@st.cache_resource(ttl=3600)
def _open_store(config):
    return get_store(config)


def _fetch_bars(symbol: str, interval: str, lookback: int):
    """Fetch bars with a short Streamlit cache to avoid hammering yfinance."""
    from drift.data.providers.yfinance_provider import YFinanceProvider
    provider = YFinanceProvider()
    try:
        return provider.get_recent_bars(symbol, interval, lookback)
    except Exception:  # noqa: BLE001
        return []
