"""Signal Detail dialog — @st.dialog modal for a single signal row."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from drift.storage.signal_store import SignalRow

_ET = ZoneInfo("America/New_York")

_OUTCOME_LABEL = {
    "TRADE_PLAN_ISSUED": ("🟢", "Trade Plan Issued",   "#4caf50"),
    "LLM_NO_TRADE":      ("🟡", "No Trade (LLM)",      "#f5a623"),
    "BLOCKED":           ("🔴", "Blocked by Gate",     "#e53935"),
}
_BIAS_COLOR = {"LONG": "#4caf50", "SHORT": "#e53935"}


@st.dialog("Signal Detail", width="large")
def show_signal_detail(sig: SignalRow) -> None:
    """Render a full signal detail modal."""
    try:
        ts = datetime.fromisoformat(sig.event_time_utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_str = ts.astimezone(_ET).strftime("%b %d %Y  %H:%M ET")
    except (ValueError, TypeError):
        ts_str = sig.event_time_utc

    icon, label, color = _OUTCOME_LABEL.get(sig.final_outcome, ("⚪", sig.final_outcome, "#888"))

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='margin-bottom:4px'>"
        f"<span style='font-size:1.15rem; font-weight:700; color:{color}'>{icon} {label}</span>"
        f"&nbsp;&nbsp;<span style='color:#888; font-size:0.85rem'>{ts_str} · {sig.symbol} · {sig.source}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.divider()

    col_left, col_right = st.columns([1.1, 0.9], gap="large")

    # ── Left: Trade Plan / LLM reasoning ──────────────────────────────────────
    with col_left:
        if sig.final_outcome == "TRADE_PLAN_ISSUED":
            bias      = (sig.bias or "—").upper()
            bias_color = _BIAS_COLOR.get(bias, "#888")
            st.markdown(
                f"<span style='font-size:1.4rem; font-weight:800; color:{bias_color}'>{bias}</span>"
                f"&nbsp;&nbsp;<span style='color:#aaa; font-size:0.9rem'>{sig.setup_type or '—'}</span>",
                unsafe_allow_html=True,
            )
            if sig.confidence is not None:
                st.progress(sig.confidence / 100, text=f"Confidence  {sig.confidence}%")

            st.markdown("**Levels**")
            m1, m2 = st.columns(2)
            if sig.entry_min and sig.entry_max:
                m1.metric("Entry Zone", f"{sig.entry_min:,.0f} – {sig.entry_max:,.0f}")
            if sig.stop_loss:
                m2.metric("Stop Loss", f"{sig.stop_loss:,.0f}")
            m3, m4 = st.columns(2)
            if sig.take_profit_1:
                m3.metric("TP 1", f"{sig.take_profit_1:,.0f}")
            if sig.take_profit_2:
                m4.metric("TP 2", f"{sig.take_profit_2:,.0f}")
            if sig.reward_risk:
                st.caption(f"Reward / Risk  {sig.reward_risk:.1f}×")

            if sig.thesis:
                st.markdown("**Thesis**")
                st.info(sig.thesis, icon="📝")

        else:
            # NO_TRADE or BLOCKED — show LLM reasoning as readable prose
            llm = sig.llm_decision
            thesis = (llm or {}).get("thesis") or sig.thesis
            conf   = (llm or {}).get("confidence")

            if conf is not None:
                st.progress(int(conf) / 100, text=f"LLM Confidence  {conf}%")

            if thesis:
                st.markdown("**LLM Reasoning**")
                st.info(thesis, icon="🤖")
            else:
                st.caption("No reasoning captured.")

        # Outcome (replay result)
        st.markdown("**Outcome**")
        if sig.replay_outcome:
            pnl_color = "normal" if (sig.pnl_points or 0) >= 0 else "inverse"
            st.metric(
                sig.replay_outcome,
                f"{sig.pnl_points:+.1f} pts" if sig.pnl_points is not None else "—",
                delta_color=pnl_color,
            )
        else:
            st.caption("Pending — not yet resolved.")

    # ── Right: Gate report ────────────────────────────────────────────────────
    with col_right:
        st.markdown("**Gate Report**")
        gate = sig.gate_report
        if gate:
            for r in gate.get("results", []):
                passed = r.get("passed", True)
                name   = r.get("gate_name") or r.get("gate", "?")
                reason = r.get("reason", "")
                icon_g = "✅" if passed else "🚫"
                st.markdown(f"{icon_g} **{name}**")
                if not passed and reason:
                    st.caption(f"   ↳ {reason}")
        else:
            st.caption("No gate report stored.")

        # Blocked reason (top-level final_reason is more informative for BLOCKED)
        if sig.final_outcome == "BLOCKED" and sig.final_reason:
            st.markdown("**Block Reason**")
            st.warning(sig.final_reason, icon="🚫")

    # ── Footer ────────────────────────────────────────────────────────────────
    st.divider()
    st.caption(f"signal_key: `{sig.signal_key}`")
