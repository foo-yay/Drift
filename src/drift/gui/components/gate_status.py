"""Gate status panel component.

Renders the last cycle's gate pass/fail results in the Live Monitor
right-hand column.  No Streamlit imports leak into business logic.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import streamlit as st

if TYPE_CHECKING:
    from drift.storage.signal_store import SignalRow

_ICON_PASS = "✅"
_ICON_FAIL = "🚫"
_ICON_WARN = "⚠️"


def render_gate_status(last_signal: "SignalRow | None") -> None:
    """Render a gate report panel from the most recent SQLite signal row."""
    st.markdown("**Last Cycle — Gate Results**")

    if last_signal is None:
        st.caption("No signals in the database yet.")
        return

    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(last_signal.event_time_utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        from zoneinfo import ZoneInfo
        ts_et = ts.astimezone(ZoneInfo("America/New_York"))
        st.caption(f"as of {ts_et.strftime('%H:%M ET')}  •  {last_signal.source}")
    except (ValueError, TypeError):
        st.caption(last_signal.event_time_utc)

    gate_report = last_signal.gate_report
    if not gate_report:
        outcome = last_signal.final_outcome
        icon = _ICON_PASS if outcome == "TRADE_PLAN_ISSUED" else _ICON_WARN
        st.markdown(f"{icon} {outcome}")
        return

    results = gate_report.get("results", [])
    for r in results:
        name   = r.get("gate", "?")
        passed = r.get("passed", True)
        reason = r.get("reason", "")
        icon   = _ICON_PASS if passed else _ICON_FAIL
        st.markdown(f"{icon} **{name}**")
        if not passed and reason:
            st.caption(f"   ↳ {reason}")


def render_last_trade_plan(last_signal: "SignalRow | None") -> None:
    """Render a compact trade plan summary below the gate status."""
    if last_signal is None or last_signal.final_outcome != "TRADE_PLAN_ISSUED":
        return

    st.divider()
    st.markdown("**Last Trade Plan**")

    bias_color = "green" if (last_signal.bias or "").upper() == "LONG" else (
        "red" if (last_signal.bias or "").upper() == "SHORT" else "gray"
    )
    st.markdown(
        f":{bias_color}[**{last_signal.bias or '—'}**] "
        f"`{last_signal.setup_type or '—'}` "
        f"conf={last_signal.confidence or '—'}%"
    )

    cols = st.columns(2)
    if last_signal.entry_min and last_signal.entry_max:
        cols[0].metric("Entry", f"{last_signal.entry_min:,.0f}–{last_signal.entry_max:,.0f}")
    if last_signal.stop_loss:
        cols[1].metric("Stop", f"{last_signal.stop_loss:,.0f}")
    cols2 = st.columns(2)
    if last_signal.take_profit_1:
        cols2[0].metric("TP1", f"{last_signal.take_profit_1:,.0f}")
    if last_signal.take_profit_2:
        cols2[1].metric("TP2", f"{last_signal.take_profit_2:,.0f}")
    if last_signal.reward_risk:
        st.caption(f"R:R  {last_signal.reward_risk:.1f}")
