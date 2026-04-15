"""Replay Lab — run historical replays and review stored results.

Features
--------
- Symbol + date-range picker
- Dedup check via store.count_by_date_range()
- Optional real-LLM / session-gate-disabled toggles
- Progress spinner while fetching bars + running the engine
- Post-run summary metrics and signal-row table
- Overwrite flow with two-step confirmation
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from drift.gui.components.signal_detail import show_signal_detail
from drift.gui.state import get_config, get_store
from drift.replay.engine import ReplayEngine
from drift.replay.loader import fetch_bars_for_date_range

_ET = ZoneInfo("America/New_York")

_OUTCOME_ICONS = {
    "TRADE_PLAN_ISSUED": "🟢",
    "LLM_NO_TRADE":      "🟡",
    "BLOCKED":           "🔴",
}


@st.cache_resource
def _load_config():
    return get_config()


@st.cache_resource
def _open_store(config):
    return get_store(config)


def page() -> None:
    st.title("🔄 Replay Lab")

    config = _load_config()
    store  = _open_store(config)

    # ------------------------------------------------------------------
    # Inputs — always visible
    # ------------------------------------------------------------------
    with st.form("replay_form"):
        col_sym, col_dates = st.columns([1, 2])

        with col_sym:
            symbol = st.text_input(
                "Symbol",
                value=config.instrument.symbol,
                help="Ticker to replay (e.g. MNQ=F).",
            )
            use_real_llm = st.checkbox("Use real LLM", value=False,
                                       help="Uncheck to use MockLLMClient (no API cost).")
            disable_session_gate = st.checkbox("Ignore session gate", value=False,
                                               help="Run pipeline at every bar regardless of market hours.")

        with col_dates:
            today = date.today()
            date_range = st.date_input(
                "Date range",
                value=(today - timedelta(days=5), today - timedelta(days=1)),
                max_value=today,
                help="yfinance 1-minute data is only available for the last 7 days.",
            )
            if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
                date_start, date_end = date_range
            else:
                date_start = date_end = date_range if date_range else today - timedelta(days=1)

        run_pressed = st.form_submit_button("▶ Run Replay", type="primary", use_container_width=True)

    # ------------------------------------------------------------------
    # Dedup check
    # ------------------------------------------------------------------
    existing_count = store.count_by_date_range(
        symbol=symbol,
        date_start=date_start,
        date_end=date_end,
        source="replay",
    )

    if existing_count:
        st.warning(
            f"⚠ **{existing_count} replay signal(s)** already exist for "
            f"**{symbol}** between {date_start} and {date_end}.",
            icon=None,
        )
        overwrite_ack = st.checkbox(
            f"Yes, delete the existing {existing_count} signal(s) and re-run",
            value=False,
            key="replay_overwrite_ack",
        )
    else:
        overwrite_ack = True  # nothing to overwrite

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    if run_pressed:
        if existing_count and not overwrite_ack:
            st.error("Tick the overwrite checkbox above to confirm deletion before re-running.")
            return

        # Delete existing signals for this range before re-running
        if existing_count and overwrite_ack:
            deleted = store.delete_by_date_range(
                symbol=symbol,
                date_start=date_start,
                date_end=date_end,
                source="replay",
            )
            st.info(f"Deleted {deleted} existing replay signal(s).", icon="🗑")

        # Fetch bars
        with st.spinner(f"Fetching bars for {symbol} ({date_start} → {date_end})…"):
            try:
                bars_1m, bars_5m, bars_1h = fetch_bars_for_date_range(
                    symbol=symbol,
                    start=date_start,
                    end=date_end,
                )
            except ValueError as exc:
                st.error(f"Could not fetch bars: {exc}")
                return

        st.success(
            f"Fetched {len(bars_1m):,} 1m bars · {len(bars_5m):,} 5m bars · {len(bars_1h):,} 1h bars",
            icon="📊",
        )

        # Build LLM client
        if use_real_llm:
            from drift.ai.client import LLMClient  # type: ignore[import]
            llm_client = LLMClient(config)
        else:
            from drift.ai.mock_client import MockLLMClient
            llm_client = MockLLMClient()

        # Run the engine
        with st.spinner("Running replay pipeline…"):
            engine = ReplayEngine(
                config=config,
                bars_1m=bars_1m,
                bars_5m=bars_5m,
                bars_1h=bars_1h,
                llm_client=llm_client,
                disable_session_gate=disable_session_gate,
            )
            summary = engine.run()

        # Persist events to store
        inserted = 0
        for evt in summary.events:
            try:
                if store.insert_event(evt):
                    inserted += 1
            except Exception:
                pass

        store.record_replay_run(
            symbol=symbol,
            date_start=date_start,
            date_end=date_end,
            signal_count=inserted,
            source="replay",
        )

        # ------------------------------------------------------------------
        # Results
        # ------------------------------------------------------------------
        st.divider()
        st.subheader("Replay Results")

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Steps processed", summary.total_steps)
        c2.metric("Pipeline steps", summary.pipeline_steps)
        c3.metric("Trade plans", summary.trade_plans_issued)
        c4.metric("No-trade", summary.llm_no_trade)
        c5.metric("Blocked", summary.blocked)
        wr = f"{summary.win_rate_pct:.1f}%" if summary.outcomes_resolved else "—"
        c6.metric("Win rate", wr)

        # Query back as SignalRow objects (needed by show_signal_detail)
        rows = store.query(
            sources=["replay"],
            date_start=date_start,
            date_end=date_end,
            order_desc=True,
        )

        if not rows:
            st.info("Replay finished — no signals were stored.", icon="🔍")
            st.session_state["_replay_rows"] = []
            return

        st.session_state["_replay_rows"] = rows

    # ------------------------------------------------------------------
    # Persistent signal table (survives reruns from pagination etc.)
    # ------------------------------------------------------------------
    rows = st.session_state.get("_replay_rows")
    if not rows:
        return

    st.divider()
    st.caption(f"{len(rows)} signals")

    tp  = sum(1 for r in rows if r.final_outcome == "TRADE_PLAN_ISSUED")
    nt  = sum(1 for r in rows if r.final_outcome == "LLM_NO_TRADE")
    blk = sum(1 for r in rows if r.final_outcome == "BLOCKED")
    bc1, bc2, bc3 = st.columns(3)
    bc1.metric("🟢 Trade plans", tp)
    bc2.metric("🟡 No-trade", nt)
    bc3.metric("🔴 Blocked", blk)

    st.divider()

    for i, sig in enumerate(rows):
        icon = _OUTCOME_ICONS.get(sig.final_outcome, "⚪")
        try:
            ts = datetime.fromisoformat(sig.event_time_utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_str = ts.astimezone(_ET).strftime("%b %-d %Y, %H:%M ET")
        except (ValueError, TypeError):
            ts_str = sig.event_time_utc or "—"

        bias_label = sig.bias or "—"
        row_label  = f"{icon}  {ts_str}  |  {sig.symbol}  |  {bias_label}"

        if st.button(row_label, key=f"replay_row_{i}", use_container_width=True):
            show_signal_detail(sig)

