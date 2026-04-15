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

import io
import warnings
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time

import streamlit as st

# Suppress yfinance/pandas deprecation noise that leaks into Streamlit logs.
warnings.filterwarnings("ignore", message="Timestamp.utcnow")

from drift.gui.components.candlestick import build_candlestick_chart
from drift.gui.components.gate_status import render_gate_status, render_last_trade_plan
from drift.gui.components.news_panel import render_news_panel
from drift.gui.components.signal_detail import show_signal_detail
from drift.gui.state import get_config, get_store

_ET = ZoneInfo("America/New_York")

# yfinance interval string per timeframe label
_TF_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "1H": "1h",
}

# Which range options to show for each timeframe
_TF_RANGES: dict[str, list[str]] = {
    "1m": ["1H", "4H", "1D", "All"],
    "5m": ["1H", "4H", "1D", "1W", "1M", "All"],
    "1H": ["1D", "1W", "1M", "All"],
}

# Default range shown when switching to a timeframe
_TF_DEFAULT_RANGE: dict[str, str] = {
    "1m": "1D",
    "5m": "1D",
    "1H": "1W",
}

# Lookback bars to fetch for each (timeframe, range) combination.
# yfinance hard limits: 1m → 7 d, 5m → 60 d, 1h → 730 d.
_RANGE_LOOKBACK: dict[str, dict[str, int]] = {
    "1m": {"1H": 60,   "4H": 240,  "1D": 390,  "All": 2700},
    "5m": {"1H": 12,   "4H": 48,   "1D": 78,   "1W": 390,  "1M": 1560, "All": 9000},
    "1H": {"1D": 24,   "1W": 168,  "1M": 744,  "All": 8760},
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

    # Show Run Now output dialog if a cycle just completed
    if st.session_state.get("_show_run_output"):
        _show_cycle_output_dialog()

    # ------------------------------------------------------------------
    # Timeframe + range selectors (outside the fragment — trigger re-fetch)
    # ------------------------------------------------------------------
    sel_col, range_col = st.columns([1, 3])
    tf = sel_col.radio(
        "Timeframe",
        list(_TF_INTERVAL.keys()),
        index=1,          # default 5m
        horizontal=True,
        label_visibility="collapsed",
        key="live_tf",
    )

    available_ranges = _TF_RANGES[tf]
    # Initialise default range for this timeframe without using default=
    # on the widget itself — mixing both causes a Streamlit warning.
    range_key = f"live_range_{tf}"
    if range_key not in st.session_state:
        st.session_state[range_key] = _TF_DEFAULT_RANGE[tf]
    range_sel = range_col.segmented_control(
        "Range",
        available_ranges,
        key=range_key,
        label_visibility="collapsed",
    ) or _TF_DEFAULT_RANGE[tf]

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
        _chart_fragment(symbol, tf, range_sel, store)

    # ------------------------------------------------------------------
    # Macro events strip (bottom)
    # ------------------------------------------------------------------
    st.divider()
    render_news_panel(filter_countries=filter_ctry)


# ---------------------------------------------------------------------------
# Chart fragment — auto-refreshes every 900 s
# ---------------------------------------------------------------------------

def _chart_fragment(symbol: str, tf: str, range_sel: str, store) -> None:
    """Wrapped in @st.fragment so it auto-refreshes independently."""

    @st.fragment(run_every=900)
    def _inner() -> None:
        interval = _TF_INTERVAL[tf]
        lookback = _RANGE_LOOKBACK[tf][range_sel]

        with st.spinner(f"Fetching {symbol} {tf} · {range_sel} ({lookback} bars)…"):
            bars = _get_bars_cached(symbol, interval, lookback)

        if not bars:
            col_warn, col_btn = st.columns([4, 1])
            col_warn.warning(
                f"No {tf} bar data returned for **{symbol}**. "
                "Yahoo Finance may be rate-limited or the market is closed. "
                "The chart will auto-refresh in 15 min.",
                icon="\u26a0\ufe0f",
            )
            if col_btn.button("Retry", key=f"retry_{tf}_{range_sel}", use_container_width=True):
                _bust_bar_cache(symbol, interval, lookback)
                st.rerun(scope="fragment")
            return

        # Signal markers: expand query window to match the loaded range
        range_days = max(7, lookback * {"1m": 1, "5m": 5, "1h": 60}.get(_TF_INTERVAL[tf], 5) // (390))
        range_start = date.today() - timedelta(days=range_days)
        try:
            signals = store.query(date_start=range_start)
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
    """Shows engine status, Run Now button, gate results, and last trade plan."""
    config = _load_config()

    # Header
    st.markdown("**Engine Status**")

    # Inject CSS once: style the single primary button on this page as
    # a subtle dark-green instead of Streamlit's default coral/red.
    st.markdown(
        """
        <style>
        div[data-testid="stButton"] button[kind="primary"] {
            background-color: #1a5c2a !important;
            border-color:     #2a7a3a !important;
            color:            #d4edda !important;
        }
        div[data-testid="stButton"] button[kind="primary"]:hover {
            background-color: #236b33 !important;
            border-color:     #33884a !important;
        }
        div[data-testid="stButton"] button[kind="primary"]:active {
            background-color: #1a5c2a !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    run_clicked = st.button("▶ Run Now", key="run_now_btn", type="primary")

    try:
        from drift.gui.state import project_root
        kill_path = project_root() / "data" / ".kill_switch"
        if kill_path.exists():
            st.error("🔴 KILL SWITCH ACTIVE", icon="🚨")
        else:
            # Compact single-line status + loop interval
            loop_secs = getattr(getattr(config, "app", None), "loop_interval_seconds", None)
            loop_label = ""
            if loop_secs:
                mins = loop_secs // 60
                secs = loop_secs % 60
                loop_label = f"  ·  auto every {mins} min" + (f" {secs:02d} s" if secs else "")
            st.markdown(
                f"<p style='margin:2px 0; color:#4caf50; font-size:0.85rem'>● Ready{loop_label}</p>",
                unsafe_allow_html=True,
            )
    except Exception:  # noqa: BLE001
        st.caption("● Status unknown")

    if run_clicked:
        _run_cycle_now(config)

    st.divider()

    try:
        recent = store.query(limit=1, order_desc=True)
        last_sig = recent[0] if recent else None
    except Exception:  # noqa: BLE001
        last_sig = None

    render_gate_status(last_sig)
    render_last_trade_plan(last_sig)


@st.dialog("Run Now — Cycle Output", width="large")
def _show_cycle_output_dialog() -> None:
    """Modal showing full Rich console output from the last ad-hoc cycle."""
    outcome = st.session_state.get("_run_outcome", "")
    output  = st.session_state.get("_run_output", "")
    error   = st.session_state.get("_run_error", "")

    if outcome == "success":
        st.success("Cycle completed — results saved to signal store.", icon="✅")
    elif outcome == "error":
        st.error(f"Cycle failed: {error}", icon="🚨")

    if output:
        st.code(output, language=None)
    else:
        st.caption("No output captured.")

    if st.button("Close", key="close_run_dialog", type="secondary"):
        st.session_state.pop("_show_run_output", None)
        st.rerun()


def _run_cycle_now(config) -> None:
    """Run a single DriftApplication cycle, capture Rich output, show dialog."""
    import drift.output.console as console_mod
    from rich.console import Console
    from drift.app import DriftApplication
    from drift.gui.state import project_root

    config_path = str(project_root() / "config" / "settings.yaml")
    # "sandbox" and "dry-run" both use MockLLMClient — consistent with CLI --dry-run flag
    _MOCK_MODES = {"sandbox", "dry-run"}
    sandbox = getattr(getattr(config, "app", None), "mode", "") in _MOCK_MODES

    # Redirect the module-level Rich console to a buffer for the duration of the run.
    buf = io.StringIO()
    capture_console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    orig_console = console_mod.console
    console_mod.console = capture_console

    outcome  = "success"
    error_msg = ""
    try:
        app = DriftApplication(config, config_path=config_path, sandbox=sandbox)
        with st.spinner("Running analysis cycle…"):
            app.run_once()
    except Exception as exc:  # noqa: BLE001
        outcome  = "error"
        error_msg = str(exc)
    finally:
        # Always restore the original console before any rerun.
        console_mod.console = orig_console

    st.session_state["_run_output"]      = buf.getvalue()
    st.session_state["_run_outcome"]     = outcome
    st.session_state["_run_error"]       = error_msg
    st.session_state["_show_run_output"] = True
    st.cache_resource.clear()  # force store to re-open on rerun
    st.rerun()  # re-render page so panel picks up new signal + dialog opens


# ---------------------------------------------------------------------------
# Cached helpers (avoid reloading every fragment run)
# ---------------------------------------------------------------------------

@st.cache_resource(ttl=3600)
def _load_config():
    return get_config()


@st.cache_resource(ttl=3600)
def _open_store(config):
    return get_store(config)


def _bar_cache_key(symbol: str, interval: str, lookback: int) -> str:
    return f"_bars_{symbol}_{interval}_{lookback}"


def _bar_ts_key(symbol: str, interval: str, lookback: int) -> str:
    return f"_bars_ts_{symbol}_{interval}_{lookback}"


def _get_bars_cached(symbol: str, interval: str, lookback: int) -> list:
    """Return bars from session-state cache, refreshing at most every 60 s.

    Crucially, a failed/empty fetch does NOT overwrite a previously good
    result — so a transient yfinance blip won't blank the chart.
    """
    cache_key = _bar_cache_key(symbol, interval, lookback)
    ts_key    = _bar_ts_key(symbol, interval, lookback)

    last_ts = st.session_state.get(ts_key, 0.0)
    if time.monotonic() - last_ts > 60:
        fresh = _fetch_bars_uncached(symbol, interval, lookback)
        st.session_state[ts_key] = time.monotonic()  # always bump throttle timer
        if fresh:  # only replace cache on success
            st.session_state[cache_key] = fresh

    return st.session_state.get(cache_key, [])


def _bust_bar_cache(symbol: str, interval: str, lookback: int) -> None:
    """Force the next call to _get_bars_cached to hit yfinance immediately."""
    ts_key = _bar_ts_key(symbol, interval, lookback)
    st.session_state.pop(ts_key, None)


def _fetch_bars_uncached(symbol: str, interval: str, lookback: int) -> list:
    """Single attempt at fetching bars — no caching, no Streamlit dependencies."""
    from drift.data.providers.yfinance_provider import YFinanceProvider
    try:
        return YFinanceProvider().get_recent_bars(symbol, interval, lookback)
    except Exception:  # noqa: BLE001
        return []
