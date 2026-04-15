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

    # Overlay toggles
    with st.expander("Overlays", expanded=False):
        ov_col1, ov_col2, ov_col3 = st.columns(3)
        show_emas   = ov_col1.checkbox("EMAs",         value=False, key="ov_emas")
        show_vwap   = ov_col2.checkbox("VWAP",         value=False, key="ov_vwap")
        show_obs    = ov_col3.checkbox("Order Blocks", value=False, key="ov_obs")

    # ------------------------------------------------------------------
    # Main layout: chart (left, 70%) + status panel (right, 30%)
    # ------------------------------------------------------------------
    chart_col, status_col = st.columns([0.70, 0.30], gap="large")

    with status_col:
        _render_status_panel(store)

    with chart_col:
        _chart_fragment(symbol, tf, range_sel, store,
                        show_emas=show_emas, show_vwap=show_vwap, show_obs=show_obs)

    # ------------------------------------------------------------------
    # Macro events strip (bottom)
    # ------------------------------------------------------------------
    st.divider()
    render_news_panel(filter_countries=filter_ctry)


# ---------------------------------------------------------------------------
# Chart fragment — auto-refreshes every 900 s
# ---------------------------------------------------------------------------

def _chart_fragment(
    symbol: str,
    tf: str,
    range_sel: str,
    store,
    show_emas: bool = False,
    show_vwap: bool = False,
    show_obs: bool = False,
) -> None:
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

        # Build overlay_data from the currently fetched bars (computed live so
        # the values always match what's visible on the chart).  Order-block and
        # rejection-block zones come from the most recent stored snapshot — they
        # reflect the last full FeatureEngine run rather than a recomputation here.
        overlay_data: dict = {}
        if show_emas or show_vwap or show_obs:
            overlay_data = _compute_overlay_data(bars, signals, show_emas, show_vwap, show_obs)

        fig = build_candlestick_chart(
            bars, signals, timeframe=tf, height=500,
            show_emas=show_emas,
            show_vwap=show_vwap,
            show_order_blocks=show_obs,
            overlay_data=overlay_data,
        )

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

_CYCLE_BADGE = {
    "TRADE_PLAN_ISSUED": ("🟢", "#4caf50", "Trade Plan"),
    "LLM_NO_TRADE":      ("🟡", "#f5a623", "No Trade"),
    "BLOCKED":           ("🔴", "#e53935", "Blocked"),
}


def _render_status_panel(store) -> None:
    """Shows engine status, Run Now button, and compact cycle history."""
    config = _load_config()

    # Header
    st.markdown("**Engine Status**")

    # Inject CSS once: style the single primary button as dark-green;
    # also prevent any button text from wrapping on narrow columns.
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
        div[data-testid="stButton"] button {
            white-space: nowrap !important;
        }
        /* Cycle-row buttons: flat left-aligned rows */
        div[data-testid="stButton"] button[data-testid="baseButton-secondary"] {
            text-align:       left !important;
            background:       transparent !important;
            border:           1px solid #2c2c2c !important;
            border-radius:    4px !important;
            color:            #ccc !important;
            padding:          4px 10px !important;
            font-size:        0.82rem !important;
            min-height:       unset !important;
            line-height:      1.5 !important;
            width:            100% !important;
            box-shadow:       none !important;
        }
        div[data-testid="stButton"] button[data-testid="baseButton-secondary"]:hover {
            background:    #161616 !important;
            border-color:  #444 !important;
            color:         #fff !important;
        }
        div[data-testid="stButton"]:has(button[data-testid="baseButton-secondary"]) {
            margin-bottom: 4px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    run_clicked = st.button("▶ Run Now", key="run_now_btn", type="primary")

    try:
        from drift.gui.state import project_root
        from drift.gui.scheduler import ensure_scheduler_running
        kill_path = project_root() / "data" / ".kill_switch"
        if kill_path.exists():
            st.error("🔴 KILL SWITCH ACTIVE", icon="🚨")
        else:
            scheduler = ensure_scheduler_running()
            _render_status_countdown(config, store, scheduler)
    except Exception:  # noqa: BLE001
        st.caption("● Status unknown")

    if run_clicked:
        _run_cycle_now(config)

    _render_cycle_history(store)


def _render_cycle_history(store) -> None:
    """Cycle history fragment — re-queries the store every 10 s.

    Defined as a top-level helper (not nested inside _render_status_panel) so
    that Streamlit can consistently identify the fragment across reruns.
    """
    @st.fragment(run_every=10)
    def _inner() -> None:
        try:
            recent = store.query(limit=10, order_desc=True)
        except Exception:  # noqa: BLE001
            recent = []

        st.divider()
        st.markdown("**Last Cycle**")
        if not recent:
            st.caption("No cycles run yet.")
        else:
            _render_cycle_row(recent[0], key="hist_0", latest=True)

        if len(recent) > 1:
            st.divider()
            st.markdown("**Recent Cycles**")
            for i, sig in enumerate(recent[1:], start=1):
                _render_cycle_row(sig, key=f"hist_{i}", latest=False)

    _inner()


def _render_status_countdown(config, store, scheduler=None) -> None:
    """Live countdown fragment refreshing every 10 s.

    Uses the scheduler's own last-run timestamp so that manual Run Now cycles
    do not reset the loop timer — only scheduled cycles advance the clock.
    Falls back to the DB if the scheduler hasn't fired yet (e.g. first load
    with existing DB records from a prior ``drift run`` session).
    """
    @st.fragment(run_every=10)
    def _inner() -> None:
        loop_secs = getattr(getattr(config, "app", None), "loop_interval_seconds", 900)

        # Scheduler health indicator
        if scheduler is not None and not scheduler.is_alive():
            st.markdown(
                "<p style='margin:2px 0; color:#e53935; font-size:0.85rem'>● Scheduler stopped</p>",
                unsafe_allow_html=True,
            )
            return

        # Prefer the scheduler's own last-run time so Run Now doesn't reset
        # the loop timer.  Fall back to the DB only if the scheduler thread
        # hasn't completed a cycle yet this session.
        last_ts: datetime | None = None
        if scheduler is not None:
            last_ts = scheduler.state.last_run_utc  # None until first scheduled cycle

        if last_ts is None:
            # Scheduler hasn't fired yet — fall back to DB so existing history
            # still drives a sensible countdown on first page load.
            try:
                recent = store.query(limit=1, order_desc=True)
                sig = recent[0] if recent else None
                if sig:
                    candidate = datetime.fromisoformat(sig.event_time_utc)
                    if candidate.tzinfo is None:
                        candidate = candidate.replace(tzinfo=timezone.utc)
                    last_ts = candidate
            except Exception:  # noqa: BLE001
                pass

        if last_ts is None:
            st.markdown(
                "<p style='margin:2px 0; color:#4caf50; font-size:0.85rem'>● Running — first cycle pending</p>",
                unsafe_allow_html=True,
            )
            return

        elapsed   = (datetime.now(tz=timezone.utc) - last_ts).total_seconds()
        remaining = loop_secs - elapsed
        if remaining <= 0:
            color, label = "#f5a623", "● Cycle running…"
        elif remaining < 30:
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            color, label = "#f5a623", f"● Running soon — {mins}:{secs:02d}"
        else:
            mins = int(remaining) // 60
            secs = int(remaining) % 60
            color, label = "#4caf50", f"● Ready · next in {mins}:{secs:02d}"
        st.markdown(
            f"<p style='margin:2px 0; color:{color}; font-size:0.85rem'>{label}</p>",
            unsafe_allow_html=True,
        )
    _inner()


def _render_cycle_row(sig, *, key: str, latest: bool) -> None:
    """Full-width clickable row for one cycle — click to open Signal Detail."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from drift.gui.components.signal_detail import show_signal_detail

    icon, _color, short_label = _CYCLE_BADGE.get(sig.final_outcome, ("⚪", "#888", sig.final_outcome))
    try:
        ts = datetime.fromisoformat(sig.event_time_utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        label_time = ts.astimezone(ZoneInfo("America/New_York")).strftime("%b %-d, %H:%M ET")
    except (ValueError, TypeError):
        label_time = "—"

    label = f"{icon} **{short_label}**   {label_time}   ›"
    if st.button(label, key=f"detail_{key}", use_container_width=True):
        show_signal_detail(sig)


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

    root = project_root()
    config_path = str(root / "config" / "settings.yaml")
    # "sandbox" and "dry-run" both use MockLLMClient — consistent with CLI --dry-run flag
    _MOCK_MODES = {"sandbox", "dry-run"}
    sandbox = getattr(getattr(config, "app", None), "mode", "") in _MOCK_MODES

    # Streamlit may run from src/ rather than the project root, so relative
    # storage paths in config (e.g. "data/local.db") would resolve to the wrong
    # location.  Absolutize them using the known project root so DriftApplication
    # writes to the same files that get_store() reads.
    abs_storage = config.storage.model_copy(update={
        "jsonl_event_log":         str(root / config.storage.jsonl_event_log),
        "sqlite_path":             str(root / config.storage.sqlite_path),
        "sandbox_jsonl_event_log": str(root / config.storage.sandbox_jsonl_event_log),
        "sandbox_sqlite_path":     str(root / config.storage.sandbox_sqlite_path),
    })
    abs_config = config.model_copy(update={"storage": abs_storage})

    # Redirect the module-level Rich console to a buffer for the duration of the run.
    buf = io.StringIO()
    capture_console = Console(file=buf, force_terminal=False, no_color=True, width=100)
    orig_console = console_mod.console
    console_mod.console = capture_console

    outcome  = "success"
    error_msg = ""
    try:
        app = DriftApplication(abs_config, config_path=config_path, sandbox=sandbox, manual_run=not sandbox)
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
# Overlay data helpers (Phase 11)
# ---------------------------------------------------------------------------

def _compute_overlay_data(
    bars: list,
    signals: list,
    want_emas: bool,
    want_vwap: bool,
    want_obs: bool,
) -> dict:
    """Compute overlay values from the visible bars + most recent stored snapshot.

    EMA and VWAP are derived from the currently fetched bars so they always
    align with what's on screen.  Order/rejection blocks come from the last
    stored MarketSnapshot (they require the full FeatureEngine run).
    """
    import pandas as pd
    from zoneinfo import ZoneInfo as _ZI

    od: dict = {}
    if not bars:
        return od

    _ETZ = _ZI("America/New_York")

    # Build a close/volume series from the bars list
    closes  = [b.close  for b in bars]
    highs   = [b.high   for b in bars]
    lows    = [b.low    for b in bars]
    volumes = [b.volume for b in bars]
    ts_list = [b.timestamp for b in bars]

    closes_s  = pd.Series(closes,  dtype=float)
    volumes_s = pd.Series(volumes, dtype=float)

    if want_emas:
        for period in (9, 21, 50):
            if len(closes_s) >= period:
                ema_val = float(closes_s.ewm(span=period, adjust=False).mean().iloc[-1])
                od[f"ema_{period}"] = ema_val

    if want_vwap:
        # Session VWAP — filter to today's RTH bars (9:30 ET onward)
        import datetime as _dt
        today_et = _dt.date.today()
        rth_open_utc = _dt.datetime(
            today_et.year, today_et.month, today_et.day,
            14, 30,  # 09:30 ET = 14:30 UTC
            tzinfo=_dt.timezone.utc,
        )
        tp = 0.0
        vol_cum = 0.0
        for i, b in enumerate(bars):
            bar_ts = b.timestamp
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.replace(tzinfo=_dt.timezone.utc)
            if bar_ts >= rth_open_utc:
                typical = (b.high + b.low + b.close) / 3
                tp      += typical * b.volume
                vol_cum += b.volume
        if vol_cum > 0:
            od["vwap"] = tp / vol_cum

    if want_obs and signals:
        latest_snap = next((s.snapshot for s in signals if s.snapshot), None)
        if latest_snap:
            od["order_blocks"]     = latest_snap.get("order_blocks", [])
            od["rejection_blocks"] = latest_snap.get("rejection_blocks", [])

    return od


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
