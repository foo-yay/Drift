"""Signal History — full auditable ledger of every recorded signal.

Features
--------
- Filter bar: symbol, date range, source multiselect, outcome multiselect
- Metric row: total signals, trade plans, win rate, total PnL
- Paginated table — click any row to open the Signal Detail dialog
- CSV export of the current filtered view
- Per-row delete via the detail dialog
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import streamlit as st

from drift.gui.components.signal_detail import show_signal_detail
from drift.gui.state import get_config, get_store

_PAGE_SIZE = 25

_OUTCOME_LABELS = {
    "TRADE_PLAN_ISSUED": "🟢 Trade Plan",
    "LLM_NO_TRADE":      "🟡 No Trade",
    "BLOCKED":           "🔴 Blocked",
}

_SOURCE_LABELS = {
    "live":    "Live",
    "sandbox": "Sandbox",
    "replay":  "Replay",
}


@st.cache_resource
def _load_config():
    return get_config()


@st.cache_resource
def _open_store(config):
    return get_store(config)


def page() -> None:
    st.title("📋 Signal History")

    config = _load_config()
    store  = _open_store(config)

    # ------------------------------------------------------------------
    # Filter bar
    # ------------------------------------------------------------------
    with st.expander("Filters", expanded=True):
        col_date, col_src, col_out = st.columns([2, 2, 2])

        with col_date:
            today = date.today()
            date_range = st.date_input(
                "Date range",
                value=(today - timedelta(days=30), today),
                max_value=today,
                key="hist_date_range",
            )
            # date_input returns a tuple when a range is selected
            if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
                date_start, date_end = date_range
            else:
                date_start = date_end = date_range if date_range else today

        with col_src:
            all_sources = ["live", "sandbox", "replay"]
            selected_sources = st.multiselect(
                "Source",
                options=all_sources,
                format_func=lambda s: _SOURCE_LABELS.get(s, s.title()),
                default=[],
                key="hist_sources",
                placeholder="All sources",
            )

        with col_out:
            all_outcomes = ["TRADE_PLAN_ISSUED", "LLM_NO_TRADE", "BLOCKED"]
            selected_outcomes = st.multiselect(
                "Outcome",
                options=all_outcomes,
                format_func=lambda o: _OUTCOME_LABELS.get(o, o),
                default=[],
                key="hist_outcomes",
                placeholder="All outcomes",
            )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    rows = store.query(
        sources=selected_sources or None,
        outcomes=selected_outcomes or None,
        date_start=date_start,
        date_end=date_end,
        limit=10_000,
        order_desc=True,
    )

    # ------------------------------------------------------------------
    # Metric row
    # ------------------------------------------------------------------
    stats  = store.win_rate_and_pnl(
        sources=selected_sources or None,
        date_start=date_start,
        date_end=date_end,
    )
    trade_plans = sum(1 for r in rows if r.final_outcome == "TRADE_PLAN_ISSUED")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Signals", len(rows))
    m2.metric("Trade Plans", trade_plans)
    m3.metric("Resolved", stats["resolved"])
    m4.metric("Win Rate", f"{stats['win_rate_pct']}%" if stats["resolved"] else "—")
    m5.metric("Total PnL", f"{stats['total_pnl']:+.1f} pts" if stats["resolved"] else "—")

    st.divider()

    if not rows:
        st.info("No signals match the current filters.", icon="🔍")
        return

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------
    col_count, col_export = st.columns([4, 1])
    col_count.caption(f"{len(rows)} signals")

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow([
        "signal_key", "symbol", "source", "event_time_utc", "final_outcome",
        "bias", "setup_type", "confidence", "entry_min", "entry_max",
        "stop_loss", "take_profit_1", "take_profit_2", "reward_risk",
        "replay_outcome", "pnl_points", "thesis",
    ])
    for r in rows:
        writer.writerow([
            r.signal_key, r.symbol, r.source, r.event_time_utc, r.final_outcome,
            r.bias, r.setup_type, r.confidence, r.entry_min, r.entry_max,
            r.stop_loss, r.take_profit_1, r.take_profit_2, r.reward_risk,
            r.replay_outcome, r.pnl_points, r.thesis,
        ])
    col_export.download_button(
        "⬇ CSV",
        data=csv_buf.getvalue(),
        file_name=f"drift_signals_{date_start}_{date_end}.csv",
        mime="text/csv",
        use_container_width=True,
    )

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------
    total_pages = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    if "hist_page" not in st.session_state:
        st.session_state["hist_page"] = 0
    # Reset to page 0 when filters change
    filter_key = (str(date_start), str(date_end), tuple(selected_sources), tuple(selected_outcomes))
    if st.session_state.get("_hist_last_filter") != filter_key:
        st.session_state["hist_page"] = 0
        st.session_state["_hist_last_filter"] = filter_key

    page_idx = st.session_state["hist_page"]
    page_rows = rows[page_idx * _PAGE_SIZE : (page_idx + 1) * _PAGE_SIZE]

    # ------------------------------------------------------------------
    # Table header
    # ------------------------------------------------------------------
    st.markdown(
        """
        <style>
        div[data-testid="stButton"] button[data-testid="baseButton-secondary"] {
            text-align:   left !important;
            background:   transparent !important;
            border:       1px solid #2a2a2a !important;
            border-radius: 3px !important;
            color:        #ccc !important;
            padding:      3px 8px !important;
            font-size:    0.78rem !important;
            min-height:   unset !important;
            line-height:  1.4 !important;
            width:        100% !important;
            box-shadow:   none !important;
            font-family:  monospace !important;
        }
        div[data-testid="stButton"] button[data-testid="baseButton-secondary"]:hover {
            background:   #1a1a1a !important;
            border-color: #555 !important;
            color:        #fff !important;
        }
        div[data-testid="stButton"]:has(button[data-testid="baseButton-secondary"]) {
            margin-bottom: 2px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    hdr = st.columns([1.2, 1.8, 0.8, 0.8, 1.0, 1.2])
    for col, label in zip(hdr, ["Outcome", "Time (ET)", "Symbol", "Source", "Bias", "Replay"]):
        col.caption(label)

    # ------------------------------------------------------------------
    # Signal rows
    # ------------------------------------------------------------------
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    for i, sig in enumerate(page_rows):
        outcome_label = _OUTCOME_LABELS.get(sig.final_outcome, sig.final_outcome)
        try:
            ts = datetime.fromisoformat(sig.event_time_utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_str = ts.astimezone(_ET).strftime("%b %-d %Y, %H:%M ET")
        except (ValueError, TypeError):
            ts_str = sig.event_time_utc

        replay_label = sig.replay_outcome or "—"
        bias_label   = sig.bias or "—"
        row_label    = f"{outcome_label}   {ts_str}   {sig.symbol}   {sig.source}   {bias_label}   {replay_label}"

        if st.button(row_label, key=f"hist_row_{page_idx}_{i}", use_container_width=True):
            show_signal_detail(sig)

    # ------------------------------------------------------------------
    # Pagination controls
    # ------------------------------------------------------------------
    if total_pages > 1:
        st.divider()
        nav_left, nav_mid, nav_right = st.columns([1, 3, 1])
        if nav_left.button("← Prev", disabled=(page_idx == 0), key="hist_prev"):
            st.session_state["hist_page"] = max(0, page_idx - 1)
            st.rerun()
        nav_mid.caption(f"Page {page_idx + 1} of {total_pages}")
        if nav_right.button("Next →", disabled=(page_idx >= total_pages - 1), key="hist_next"):
            st.session_state["hist_page"] = min(total_pages - 1, page_idx + 1)
            st.rerun()

