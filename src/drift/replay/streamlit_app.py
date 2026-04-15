"""Drift Replay GUI — Streamlit visual frontend for the replay engine.

Run from the project root:
    streamlit run src/drift/replay/streamlit_app.py

Or via the CLI alias:
    drift replay-gui
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import streamlit as st

from drift.replay.chart import build_chart, events_to_df
from drift.replay.engine import ReplayEngine, ReplaySummary
from drift.replay.loader import fetch_bars_for_date_range
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


# ------------------------------------------------------------------
# Cached data loading
# ------------------------------------------------------------------

@st.cache_data(show_spinner="Fetching bars and running replay…")
def _run_replay(
    symbol: str,
    start: date,
    end: date,
    disable_session_gate: bool,
) -> tuple[ReplaySummary, list]:
    config = load_app_config(_CONFIG_PATH)
    bars_1m, bars_5m, bars_1h = fetch_bars_for_date_range(symbol, start, end)
    engine = ReplayEngine(
        config=config,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_1h=bars_1h,
        disable_session_gate=disable_session_gate,
        verbose=False,
    )
    return engine.run(), bars_1m


# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------

with st.sidebar:
    st.title("📈 Drift Replay")
    st.caption("Visual replay frontend — no data leaves your machine.")
    st.divider()

    st.subheader("Date Range")
    today = date.today()
    start_date = st.date_input("Start", today - timedelta(days=5), key="start")
    end_date   = st.date_input("End",   today - timedelta(days=1), key="end")

    st.subheader("Options")
    disable_session = st.checkbox(
        "Disable session gate",
        value=False,
        help="Show signals outside RTH hours (useful for overnight / pre-market review).",
    )

    st.divider()
    run_btn = st.button("▶ Run Replay", type="primary", use_container_width=True)
    st.caption(
        "yfinance 1-minute data is only available for the last 7 days. "
        "For older dates, use `drift replay` with CSV files."
    )

# ------------------------------------------------------------------
# Main content
# ------------------------------------------------------------------

st.title("Replay")

if run_btn:
    if start_date > end_date:
        st.error("Start date must be before end date.")
        st.stop()
    # Clear cache so changing dates always re-fetches
    _run_replay.clear()
    try:
        summary, bars_1m = _run_replay("MNQ", start_date, end_date, disable_session)
        st.session_state["summary"] = summary
        st.session_state["bars_1m"] = bars_1m
        st.session_state.pop("selected_idx", None)
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

if "summary" not in st.session_state:
    st.info("👈 Configure a date range in the sidebar and click **▶ Run Replay**.")
    st.stop()

summary: ReplaySummary = st.session_state["summary"]
bars_1m = st.session_state["bars_1m"]
trade_events = [e for e in summary.events if e.final_outcome == "TRADE_PLAN_ISSUED"]

# ------------------------------------------------------------------
# Metric cards
# ------------------------------------------------------------------

total_pnl = sum(
    (e.replay_outcome or {}).get("pnl_points", 0.0) for e in trade_events
)
pnl_sign = "+" if total_pnl >= 0 else ""

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Pipeline steps",  summary.pipeline_steps)
c2.metric("Signals issued",  summary.trade_plans_issued)
c3.metric("Blocked",         summary.blocked)
c4.metric("LLM NO_TRADE",    summary.llm_no_trade)
c5.metric("Win rate",        f"{summary.win_rate_pct}%")
c6.metric("Total PnL",      f"{pnl_sign}{total_pnl:.1f} pts")

st.divider()

# ------------------------------------------------------------------
# Chart
# ------------------------------------------------------------------

selected_idx = st.session_state.get("selected_idx")
fig = build_chart(bars_1m, summary.events, selected_idx)
st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------------
# Signal table + detail panel
# ------------------------------------------------------------------

df = events_to_df(summary.events)

if df.empty:
    st.info("No trade plans were issued in this period. Try disabling the session gate or widening the date range.")
    st.stop()

st.subheader(f"Signals ({len(df)})")
selection = st.dataframe(
    df,
    use_container_width=True,
    selection_mode="single-row",
    on_select="rerun",
    key="signal_table",
    hide_index=True,
)

selected_rows = selection.selection.rows if selection and selection.selection else []

if selected_rows:
    idx = selected_rows[0]
    st.session_state["selected_idx"] = idx
    event = trade_events[idx]
    tp   = event.trade_plan or {}
    out  = event.replay_outcome or {}
    gates = event.pre_gate_report or {}
    llm   = event.llm_decision_parsed or {}

    st.divider()
    st.subheader("Signal Detail")

    col_plan, col_outcome, col_gates = st.columns(3)

    with col_plan:
        st.markdown("**Trade Plan**")
        st.json({
            "bias":         tp.get("bias"),
            "setup_type":   tp.get("setup_type"),
            "confidence":   tp.get("confidence"),
            "entry_zone":  f"{tp.get('entry_min')} – {tp.get('entry_max')}",
            "stop_loss":    tp.get("stop_loss"),
            "take_profit_1": tp.get("take_profit_1"),
            "take_profit_2": tp.get("take_profit_2"),
            "reward_risk":  tp.get("reward_risk_ratio"),
            "max_hold_min": tp.get("max_hold_minutes"),
            "thesis":       tp.get("thesis"),
        })

    with col_outcome:
        st.markdown("**Outcome**")
        st.json(out)
        st.markdown("**LLM Decision**")
        st.json({k: v for k, v in llm.items() if k not in ("entry_zone", "do_not_trade_if")})

    with col_gates:
        st.markdown("**Gate Report**")
        st.json(gates)
