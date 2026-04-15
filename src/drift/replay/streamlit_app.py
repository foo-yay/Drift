"""Drift Replay GUI — Streamlit visual frontend for the replay engine.

Run from the project root:
    streamlit run src/drift/replay/streamlit_app.py

Or via the CLI alias:
    drift replay-gui
"""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Load .env before any module that reads ANTHROPIC_API_KEY.
# Streamlit runs this file directly (bypassing cli.py where load_dotenv() normally fires).
load_dotenv()

import streamlit as st
import plotly.graph_objects as go

from drift.replay.chart import build_chart, events_to_df
from drift.replay.engine import ReplayEngine, ReplaySummary
from drift.replay.loader import fetch_bars_for_date_range, load_bars_from_csv, save_bars_to_csv
from drift.storage.reader import load_events_from_log
from drift.utils.config import load_app_config

# ------------------------------------------------------------------
# Page config (must be first Streamlit call)
# ------------------------------------------------------------------
st.set_page_config(
    page_title="Drift — Replay GUI",
    page_icon="📈",
    layout="wide",
)

_CONFIG_PATH = Path(__file__).parents[3] / "config" / "settings.yaml"
_LOG_PATH    = Path(__file__).parents[3] / "logs" / "events.jsonl"
_DATA_DIR    = Path(__file__).parents[3] / "data"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _summary_from_events(events: list) -> ReplaySummary:
    """Reconstruct aggregate counts from a flat event list (for browse / csv mode)."""
    s = ReplaySummary()
    s.pipeline_steps = len(events)
    s.events = list(events)
    for e in events:
        if e.final_outcome == "BLOCKED":
            s.blocked += 1
        elif e.final_outcome == "LLM_NO_TRADE":
            s.llm_no_trade += 1
        elif e.final_outcome == "TRADE_PLAN_ISSUED":
            s.trade_plans_issued += 1
            o = (e.replay_outcome or {}).get("outcome", "")
            if o == "TP1_HIT":
                s.tp1_hits += 1
            elif o == "TP2_HIT":
                s.tp2_hits += 1
            elif o == "STOP_HIT":
                s.stop_hits += 1
            elif o == "TIME_STOP":
                s.time_stops += 1
            elif o == "SESSION_END":
                s.session_ends += 1
    return s


# ------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------

@st.cache_resource(show_spinner="Fetching bars and running replay…")
def _run_replay(
    symbol: str,
    start: date,
    end: date,
    disable_session_gate: bool,
    dry_run: bool,
) -> tuple[ReplaySummary, list]:
    import os
    from drift.ai.client import LLMClient
    from drift.ai.mock_client import MockLLMClient

    config = load_app_config(_CONFIG_PATH)
    bars_1m, bars_5m, bars_1h = fetch_bars_for_date_range(symbol, start, end)

    # Auto-save fetched bars so they can be reused via CSV Replay without re-fetching.
    tag = f"{symbol}_{start}_{end}"
    save_bars_to_csv(bars_1m, _DATA_DIR / f"{tag}_1m.csv")
    save_bars_to_csv(bars_5m, _DATA_DIR / f"{tag}_5m.csv")
    save_bars_to_csv(bars_1h, _DATA_DIR / f"{tag}_1h.csv")

    llm_client = (
        MockLLMClient()
        if dry_run or not os.environ.get("ANTHROPIC_API_KEY")
        else LLMClient(config.llm)
    )
    engine = ReplayEngine(
        config=config,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_1h=bars_1h,
        llm_client=llm_client,
        disable_session_gate=disable_session_gate,
        verbose=False,
    )
    return engine.run(), bars_1m


def _run_csv_replay(
    bytes_1m: bytes,
    bytes_5m: bytes,
    bytes_1h: bytes,
    symbol: str,
    disable_session_gate: bool,
    dry_run: bool,
) -> tuple[ReplaySummary, list]:
    import os
    from drift.ai.client import LLMClient
    from drift.ai.mock_client import MockLLMClient

    config = load_app_config(_CONFIG_PATH)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "1m.csv").write_bytes(bytes_1m)
        (tmp / "5m.csv").write_bytes(bytes_5m)
        (tmp / "1h.csv").write_bytes(bytes_1h)
        bars_1m, bars_5m, bars_1h = load_bars_from_csv(
            tmp / "1m.csv", tmp / "5m.csv", tmp / "1h.csv", symbol
        )

    llm_client = (
        MockLLMClient()
        if dry_run or not os.environ.get("ANTHROPIC_API_KEY")
        else LLMClient(config.llm)
    )
    engine = ReplayEngine(
        config=config,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_1h=bars_1h,
        llm_client=llm_client,
        disable_session_gate=disable_session_gate,
        verbose=False,
    )
    return engine.run(), bars_1m


# ------------------------------------------------------------------
# Trade detail dialog
# ------------------------------------------------------------------

@st.dialog("Signal Detail", width="large")
def _show_signal_detail(event) -> None:
    tp    = event.trade_plan or {}
    out   = event.replay_outcome or {}
    gates = event.pre_gate_report or {}
    llm   = event.llm_decision_parsed or {}

    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("**Trade Plan**")
        st.json({
            "bias":          tp.get("bias"),
            "setup_type":    tp.get("setup_type"),
            "confidence":    tp.get("confidence"),
            "entry_zone":   f"{tp.get('entry_min')} – {tp.get('entry_max')}",
            "stop_loss":     tp.get("stop_loss"),
            "take_profit_1": tp.get("take_profit_1"),
            "take_profit_2": tp.get("take_profit_2"),
            "reward_risk":   tp.get("reward_risk_ratio"),
            "max_hold_min":  tp.get("max_hold_minutes"),
            "thesis":        tp.get("thesis"),
        })
        st.markdown("**LLM Decision**")
        st.json({k: v for k, v in llm.items() if k not in ("entry_zone", "do_not_trade_if")})

    with col_right:
        st.markdown("**Outcome**")
        st.json(out)
        st.markdown("**Gate Report**")
        st.json(gates)


# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------

with st.sidebar:
    st.title("📈 Drift")
    st.divider()

    mode = st.radio(
        "Mode",
        ["▶ Run Replay", "📁 CSV Replay", "📂 Browse History"],
        index=0,
    )
    st.divider()

    show_dry_run = st.checkbox(
        "Show dry-run signals",
        value=False,
        key="show_dry_run",
        help="Show signals generated with --dry-run (mock LLM). Only relevant in Browse History.",
    )

    run_btn = False

    if mode == "▶ Run Replay":
        st.subheader("Date Range")
        today      = date.today()
        start_date = st.date_input("Start", today - timedelta(days=5), key="yf_start")
        end_date   = st.date_input("End",   today - timedelta(days=1), key="yf_end")
        st.subheader("Options")
        disable_session = st.checkbox("Disable session gate", value=False, key="yf_ds")
        dry_run = st.checkbox(
            "Dry run (mock LLM)", value=False, key="yf_dr",
            help="No API credits spent. Auto-enabled when ANTHROPIC_API_KEY is not set.",
        )
        st.divider()
        run_btn = st.button("▶ Run Replay", type="primary", use_container_width=True)
        st.caption("yfinance 1m data is only available for the last 7 days.")

    elif mode == "📁 CSV Replay":
        st.subheader("CSV Files")
        st.caption("Required columns: `timestamp, open, high, low, close, volume`")
        f_1m = st.file_uploader("1-minute bars", type="csv", key="csv_1m")
        f_5m = st.file_uploader("5-minute bars", type="csv", key="csv_5m")
        f_1h = st.file_uploader("1-hour bars",   type="csv", key="csv_1h")
        st.subheader("Options")
        disable_session = st.checkbox("Disable session gate", value=False, key="csv_ds")
        dry_run         = st.checkbox("Dry run (mock LLM)",   value=False, key="csv_dr")
        st.divider()
        run_btn = st.button("▶ Run CSV Replay", type="primary", use_container_width=True)

    elif mode == "📂 Browse History":
        st.caption("Source: `logs/events.jsonl`")
        if not _LOG_PATH.exists():
            st.warning("No events.jsonl found. Run a replay first to populate the log.")
        else:
            _all = load_events_from_log(_LOG_PATH)
            _tpi = [e for e in _all if e.final_outcome == "TRADE_PLAN_ISSUED"]
            _by_src: dict[str, int] = {}
            for _e in _tpi:
                _src = getattr(_e, "source", "live")
                _by_src[_src] = _by_src.get(_src, 0) + 1
            _resolved = sum(1 for e in _tpi if e.replay_outcome)
            _pending  = sum(1 for e in _tpi if not e.replay_outcome)
            lines = [f"{len(_all)} events total"]
            for _src, _cnt in sorted(_by_src.items()):
                lines.append(f"  {_src}: {_cnt} signal(s)")
            lines += [f"✅ {_resolved} resolved", f"📌 {_pending} pending"]
            st.caption("  \n".join(lines))
        run_btn = st.button("🔄 Load / Refresh", type="primary", use_container_width=True)


# ------------------------------------------------------------------
# Run handlers
# ------------------------------------------------------------------

st.title("Drift — Replay")

if run_btn:
    if mode == "▶ Run Replay":
        if start_date > end_date:
            st.error("Start date must be before end date.")
            st.stop()
        _run_replay.clear()
        try:
            summary, bars_1m = _run_replay("MNQ", start_date, end_date, disable_session, dry_run)
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        st.session_state["events"]  = summary.events
        st.session_state["bars_1m"] = bars_1m

    elif mode == "📁 CSV Replay":
        if not (f_1m and f_5m and f_1h):
            st.error("Please upload all three CSV files before running.")
            st.stop()
        with st.spinner("Loading CSVs and running replay…"):
            try:
                summary, bars_1m = _run_csv_replay(
                    f_1m.read(), f_5m.read(), f_1h.read(),
                    "MNQ", disable_session, dry_run,
                )
            except Exception as exc:
                st.error(f"CSV replay failed: {exc}")
                st.stop()
        st.session_state["events"]  = summary.events
        st.session_state["bars_1m"] = bars_1m

    elif mode == "📂 Browse History":
        events = load_events_from_log(_LOG_PATH)
        if not events:
            st.warning("No events found in logs/events.jsonl. Run a replay first.")
            st.stop()
        st.session_state["events"]  = events
        st.session_state["bars_1m"] = None

    st.session_state.pop("selected_idx",      None)
    st.session_state.pop("_dialog_shown_for", None)
    st.session_state.pop("page",              None)


# ------------------------------------------------------------------
# Guard — nothing loaded yet
# ------------------------------------------------------------------

if "events" not in st.session_state:
    if mode == "📂 Browse History":
        st.info("Click **🔄 Load / Refresh** to read past results from the event log.")
    else:
        st.info("👈 Configure settings in the sidebar and click **▶ Run Replay**.")
    st.stop()

events  = st.session_state["events"]
bars_1m = st.session_state.get("bars_1m")   # None in browse mode

# Apply dry-run filter at display time so toggling doesn't require a reload.
if not st.session_state.get("show_dry_run", False):
    events = [e for e in events if getattr(e, "source", "live") != "dry_run"]

summary      = _summary_from_events(events)
trade_events = [e for e in events if e.final_outcome == "TRADE_PLAN_ISSUED" and e.trade_plan]


# ------------------------------------------------------------------
# Metric cards
# ------------------------------------------------------------------

total_pnl = sum((e.replay_outcome or {}).get("pnl_points", 0.0) for e in trade_events)
pnl_sign  = "+" if total_pnl >= 0 else ""

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Pipeline steps", summary.pipeline_steps)
c2.metric("Signals issued", summary.trade_plans_issued)
c3.metric("Blocked",        summary.blocked)
c4.metric("LLM NO_TRADE",   summary.llm_no_trade)
c5.metric("Win rate",       f"{summary.win_rate_pct}%")
c6.metric("Total PnL",     f"{pnl_sign}{total_pnl:.1f} pts")

st.divider()


# ------------------------------------------------------------------
# Candlestick chart (skipped in browse mode — no bars stored in log)
# ------------------------------------------------------------------

selected_idx = st.session_state.get("selected_idx")

if bars_1m:
    fig = build_chart(bars_1m, events, selected_idx)
    st.plotly_chart(fig, use_container_width=True)
elif mode == "📂 Browse History":
    # Equity curve — cumulative PnL of resolved signals over time
    resolved_pnl = [
        (e.event_time, float((e.replay_outcome or {}).get("pnl_points", 0.0)))
        for e in trade_events if e.replay_outcome
    ]
    if resolved_pnl:
        resolved_pnl.sort(key=lambda x: x[0])
        times = [r[0] for r in resolved_pnl]
        running: list[float] = []
        total = 0.0
        for _, pnl in resolved_pnl:
            total += pnl
            running.append(round(total, 2))
        curve_color = "#00b386" if total >= 0 else "#e05252"
        fill_color  = "rgba(0,179,134,0.1)" if total >= 0 else "rgba(224,82,82,0.1)"
        eq_fig = go.Figure(go.Scatter(
            x=times,
            y=running,
            mode="lines+markers",
            line=dict(color=curve_color, width=2),
            marker=dict(size=5, color=curve_color),
            fill="tozeroy",
            fillcolor=fill_color,
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>Cumulative PnL: %{y:.1f} pts<extra></extra>",
        ))
        eq_fig.update_layout(
            height=260,
            title=f"Equity Curve — {len(resolved_pnl)} resolved signal(s)",
            paper_bgcolor="#111111",
            plot_bgcolor="#111111",
            font=dict(color="#cccccc", size=11),
            xaxis=dict(gridcolor="#1e1e1e"),
            yaxis=dict(gridcolor="#1e1e1e", title="Cumulative PnL (pts)"),
            margin=dict(l=10, r=10, t=40, b=10),
        )
        st.plotly_chart(eq_fig, use_container_width=True)
    else:
        st.info(
            "No resolved signals yet — run `drift backfill-outcomes` to resolve live signals, "
            "or run a replay to log new ones."
        )


# ------------------------------------------------------------------
# Signal table
# ------------------------------------------------------------------

df = events_to_df(trade_events)

if df.empty:
    st.info(
        "No trade plans were issued. "
        "Try disabling the session gate, widening the date range, or using CSV Replay."
    )
    st.stop()

# Pagination
_PAGE_SIZE  = 25
_total_pages = max(1, (len(df) - 1) // _PAGE_SIZE + 1)
if st.session_state.get("page", 0) >= _total_pages:
    st.session_state["page"] = 0
_page = st.session_state.get("page", 0)
_start = _page * _PAGE_SIZE
_end   = min(_start + _PAGE_SIZE, len(df))

st.subheader(f"Signals ({len(df)})")
col_prev, col_info, col_next = st.columns([1, 3, 1])
with col_prev:
    if st.button("← Prev", disabled=(_page == 0), key="pg_prev"):
        st.session_state["page"] = _page - 1
        st.session_state.pop("_dialog_shown_for", None)
        st.rerun()
with col_info:
    st.caption(f"Page {_page + 1} of {_total_pages}  ({_start + 1}–{_end} of {len(df)})")
with col_next:
    if st.button("Next →", disabled=(_end >= len(df)), key="pg_next"):
        st.session_state["page"] = _page + 1
        st.session_state.pop("_dialog_shown_for", None)
        st.rerun()

df_page = df.iloc[_start:_end]
selection = st.dataframe(
    df_page,
    use_container_width=True,
    selection_mode="single-row",
    on_select="rerun",
    key="signal_table",
    hide_index=True,
)

selected_rows = selection.selection.rows if selection and selection.selection else []

if selected_rows:
    absolute_idx = _start + selected_rows[0]
    st.session_state["selected_idx"] = absolute_idx
    if st.session_state.get("_dialog_shown_for") != absolute_idx:
        st.session_state["_dialog_shown_for"] = absolute_idx
        _show_signal_detail(trade_events[absolute_idx])
