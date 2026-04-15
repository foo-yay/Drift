"""Signal Detail dialog — @st.dialog modal for a single signal row."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from drift.storage.signal_store import SignalRow

_ET = ZoneInfo("America/New_York")


@st.dialog("Signal Detail", width="large")
def show_signal_detail(sig: SignalRow) -> None:
    """Render a full signal detail modal.

    Call as ``show_signal_detail(row)`` — Streamlit will overlay the dialog
    on top of the current page.
    """
    try:
        ts = datetime.fromisoformat(sig.event_time_utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_str = ts.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET")
    except (ValueError, TypeError):
        ts_str = sig.event_time_utc

    _outcome_badge = {
        "TRADE_PLAN_ISSUED": "🟢",
        "LLM_NO_TRADE":      "🔵",
        "BLOCKED":           "🔴",
    }.get(sig.final_outcome, "⚪")

    st.caption(
        f"{ts_str}  •  **{sig.symbol}**  •  "
        f"source: `{sig.source}`  •  "
        f"{_outcome_badge} {sig.final_outcome}"
    )

    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("**Trade Plan**")
        if sig.final_outcome == "TRADE_PLAN_ISSUED":
            st.json({
                "bias":          sig.bias,
                "setup_type":    sig.setup_type,
                "confidence":    f"{sig.confidence}%" if sig.confidence else None,
                "entry_zone":    f"{sig.entry_min} – {sig.entry_max}" if sig.entry_min else None,
                "stop_loss":     sig.stop_loss,
                "take_profit_1": sig.take_profit_1,
                "take_profit_2": sig.take_profit_2,
                "reward_risk":   sig.reward_risk,
                "thesis":        sig.thesis,
            })
        else:
            st.info("No trade plan — signal was blocked or LLM said NO_TRADE.")

        st.markdown("**Outcome**")
        if sig.replay_outcome:
            pnl_col = "normal" if (sig.pnl_points or 0) >= 0 else "inverse"
            st.metric("Result", sig.replay_outcome,
                      delta=f"{sig.pnl_points:+.1f} pts" if sig.pnl_points is not None else None,
                      delta_color=pnl_col)
        else:
            st.caption("PENDING — no outcome resolved yet")

    with col_right:
        st.markdown("**Gate Report**")
        gate = sig.gate_report
        if gate:
            for r in gate.get("results", []):
                icon = "✅" if r.get("passed") else "🚫"
                st.markdown(f"{icon} **{r.get('gate', '?')}**")
                if not r.get("passed") and r.get("reason"):
                    st.caption(f"   ↳ {r['reason']}")
        else:
            st.caption("No gate report stored.")

        st.markdown("**LLM Decision**")
        llm = sig.llm_decision
        if llm:
            display_keys = ("decision", "bias", "setup_type", "confidence", "thesis")
            st.json({k: llm[k] for k in display_keys if k in llm})
        else:
            st.caption("No LLM response stored.")

    # Signal key footer
    st.divider()
    st.caption(f"`signal_key: {sig.signal_key}`")
