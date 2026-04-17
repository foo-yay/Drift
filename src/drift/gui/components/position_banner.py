"""Global banner — pending approvals + active positions on every page.

Call ``render_position_banner()`` from ``gui/app.py`` (after page config, before
navigation) so it appears at the top of every Streamlit page regardless of which
page the user is viewing.

Renders one card per pending approval and one card per open position with action
buttons inline. Auto-refreshes every 15 s via st.fragment without a full page
reload.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import streamlit as st

from drift.gui.state import get_config, _PROJECT_ROOT

log = logging.getLogger(__name__)

_BIAS_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}
_MODE_BADGE = {
    "TP1":         "🎯 TP1",
    "TP2":         "🎯🎯 TP2",
    "MANUAL":      "✋ Hold (indefinite)",
    "HOLD_EXPIRY": "⏰ Hold to expiry",
}


def _time_display(pos) -> str:
    """Return a time string for the hold window."""
    if not (pos.fill_time and pos.max_hold_minutes):
        return ""
    try:
        fill_dt = datetime.fromisoformat(pos.fill_time)
        if fill_dt.tzinfo is None:
            fill_dt = fill_dt.replace(tzinfo=timezone.utc)
        elapsed_min = (datetime.now(tz=timezone.utc) - fill_dt).total_seconds() / 60
        remaining = pos.max_hold_minutes - elapsed_min
        if remaining > 0:
            return f"⏱ {remaining:.0f}m"
        over = abs(remaining)
        if pos.exit_mode == "MANUAL":
            return f"✋ +{over:.0f}m past window"
        if pos.exit_mode == "HOLD_EXPIRY":
            return "⏰ closing..."
        return f"⚠️ +{over:.0f}m past window"
    except (ValueError, TypeError):
        return ""


@st.fragment(run_every=15)
def render_position_banner() -> None:
    """Render pending approvals + open positions at the top of every page."""
    config = get_config()
    if not config.broker.enabled:
        return

    try:
        from drift.storage.position_store import PositionStore
        from drift.storage.pending_order_store import PendingOrderStore

        db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)

        pos_store = PositionStore(db_path)
        open_positions = pos_store.get_open()
        pos_store.close()

        pend_store = PendingOrderStore(db_path)
        pend_store.expire_stale(config.broker.approval_expiry_minutes)
        pending_orders = pend_store.get_pending()
        pend_store.close()
    except Exception:  # noqa: BLE001
        return

    if not open_positions and not pending_orders:
        return

    for order in pending_orders:
        _render_pending_banner_card(config, order)

    for pos in open_positions:
        _render_position_card(config, pos)


def _render_pending_banner_card(config, order) -> None:
    """Compact pending approval card shown in the global banner."""
    bias_emoji = _BIAS_EMOJI.get(order.bias, "")

    time_warning = ""
    if order.generated_at:
        try:
            gen = datetime.fromisoformat(order.generated_at)
            if gen.tzinfo is None:
                gen = gen.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(tz=timezone.utc) - gen).total_seconds() / 60
            if elapsed > order.max_hold_minutes:
                time_warning = " ⚠️ expired"
            elif elapsed > order.max_hold_minutes * 0.7:
                time_warning = f" ⏰ {order.max_hold_minutes - elapsed:.0f}m left"
        except (ValueError, TypeError):
            pass

    tp2_str = f"{order.take_profit_2:.2f}" if order.take_profit_2 else "—"
    entry_str = f"{order.entry_min:.2f}–{order.entry_max:.2f}"

    with st.container(border=True):
        c0, c1, c2, c3 = st.columns([2, 3, 1.2, 1.2], vertical_alignment="top")
        c0.markdown(
            f"⏳ {bias_emoji} **{order.bias} {order.symbol}**  \n"
            f"<small style='color:#aaa'>`{order.setup_type}` · {order.confidence}%"
            f"{time_warning}</small>",
            unsafe_allow_html=True,
        )
        c1.markdown(
            f"<small style='color:#aaa'>Entry</small> **{entry_str}** &ensp;"
            f"<small style='color:#e05252'>SL</small> **{order.stop_loss:.2f}**<br>"
            f"<small style='color:#52b788'>TP1</small> **{order.take_profit_1:.2f}** &ensp;"
            f"<small style='color:#52b788'>TP2</small> **{tp2_str}**",
            unsafe_allow_html=True,
        )
        if c2.button("✅ Approve", key=f"bn_approve_{order.id}", type="primary"):
            _approve_order(config, order)
        if c2.button("❌ Reject", key=f"bn_reject_{order.id}"):
            _reject_order(config, order)
        if c3.button("🧠 Assess", key=f"bn_assess_pend_{order.id}"):
            st.info("LLM assessment for pending orders is not yet implemented.", icon="🧠")


def _render_position_card(config, pos) -> None:
    """One card per open position: identity | prices (2 rows) | P&L | buttons."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")
    state_str = "⏳" if pos.state == "WORKING" else "📊"
    entry_str = f"{pos.entry_fill:.2f}" if pos.entry_fill else f"lim {pos.entry_limit:.2f}"
    mode_badge = _MODE_BADGE.get(pos.exit_mode, pos.exit_mode)
    tp2_str = f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—"
    time_md = _time_display(pos)

    # P&L — single-line HTML span so it never wraps onto two lines
    pnl_md = ""
    if pos.entry_fill:
        try:
            from drift.data.providers.yfinance_provider import YFinanceProvider
            current_price = YFinanceProvider().get_latest_quote(pos.symbol)
            pts = (current_price - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current_price)
            usd = pts * 0.50 * pos.quantity
            clr = "#52b788" if pts >= 0 else "#e05252"
            pnl_md = (
                f"<span style='color:{clr};white-space:nowrap'>"
                f"{pts:+.2f} pts&nbsp;(${usd:+.2f})</span>"
            )
        except Exception:  # noqa: BLE001
            pnl_md = "<span style='color:#aaa'>P&L —</span>"

    # Button slots — Hold is one popover regardless of current hold sub-mode
    if pos.state == "FILLED":
        btn_labels: list[str] = []
        if pos.exit_mode != "TP1" and pos.take_profit_1:
            btn_labels.append("tp1")
        if pos.exit_mode != "TP2" and pos.take_profit_2:
            btn_labels.append("tp2")
        btn_labels += ["hold", "close", "assess"]
    else:
        btn_labels = ["cancel"]

    # CSS: tighten gap between button columns to ~5 px
    _BTN_CSS = (
        "<style>[data-testid='stHorizontalBlock']"
        "{gap:5px!important;}</style>"
    )

    col_widths = [2, 3, 2] + [1.0] * len(btn_labels)

    with st.container(border=True):
        st.markdown(_BTN_CSS, unsafe_allow_html=True)
        cols = st.columns(col_widths, vertical_alignment="top")
        c0, c1, c2 = cols[0], cols[1], cols[2]
        btn_cols = cols[3:]

        # Col 0: "📊 🟢 LONG MNQ" on line 1; mode badge + time on line 2
        time_part = (
            f"&nbsp;&nbsp;<span style='color:#666'>{time_md}</span>"
            if time_md else ""
        )
        c0.markdown(
            f"{state_str} {bias_emoji} **{pos.bias} {pos.symbol}**<br>"
            f"<small style='color:#aaa'>{mode_badge}</small>{time_part}",
            unsafe_allow_html=True,
        )

        # Col 1: Entry/SL line 1, TP1/TP2 line 2
        c1.markdown(
            f"<small style='color:#aaa'>Entry</small> **{entry_str}** &ensp;"
            f"<small style='color:#e05252'>SL</small> **{pos.stop_loss:.2f}**<br>"
            f"<small style='color:#52b788'>TP1</small> **{pos.take_profit_1:.2f}** &ensp;"
            f"<small style='color:#52b788'>TP2</small> **{tp2_str}**",
            unsafe_allow_html=True,
        )

        # Col 2: P&L — single line
        if pnl_md:
            c2.markdown(pnl_md, unsafe_allow_html=True)

        # Buttons — inline, right side
        i = 0
        if pos.state == "FILLED":
            if "tp1" in btn_labels:
                if btn_cols[i].button("→TP1", key=f"bn_tp1_{pos.id}",
                                      help=f"Switch exit to TP1 @ {pos.take_profit_1:.2f}"):
                    _switch_mode(config, pos.id, "TP1")
                i += 1
            if "tp2" in btn_labels:
                if btn_cols[i].button("→TP2", key=f"bn_tp2_{pos.id}",
                                      help=f"Switch exit to TP2 @ {pos.take_profit_2:.2f}"):
                    _switch_mode(config, pos.id, "TP2")
                i += 1
            # Single Hold button → popover with two sub-options
            with btn_cols[i].popover("✋ Hold"):
                st.markdown("**Choose hold mode**")
                if st.button(
                    "✋ Hold indefinitely",
                    key=f"bn_hold_indef_{pos.id}",
                    disabled=(pos.exit_mode == "MANUAL"),
                    help="Cancel TP on IB. Position stays open until you close it or SL fires. Time window is ignored.",
                ):
                    _switch_mode(config, pos.id, "MANUAL")
                if st.button(
                    "⏰ Hold to expiry",
                    key=f"bn_hold_exp_{pos.id}",
                    disabled=(pos.exit_mode == "HOLD_EXPIRY"),
                    help="Cancel TP on IB. Auto-closes at time window. SL is still active.",
                ):
                    _switch_mode(config, pos.id, "HOLD_EXPIRY")
            i += 1
            if btn_cols[i].button("✕ Close", key=f"bn_close_{pos.id}",
                                  help="Submit market order to close now"):
                _manual_close(config, pos.id)
            i += 1
            if btn_cols[i].button("🧠 Assess", key=f"bn_assess_{pos.id}"):
                _assess_position(config, pos)
        else:
            if btn_cols[0].button("🚫 Cancel", key=f"bn_cancel_{pos.id}",
                                  help="Cancel working entry order"):
                _manual_close(config, pos.id)


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------

def _switch_mode(config, position_id: int, mode: str) -> None:
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)
    result = mgr.switch_exit_mode(position_id, mode)
    mgr.close()
    if result["status"] == "ok":
        st.toast(f"Exit mode switched to {mode}")
    else:
        st.error(f"Failed: {result.get('message', 'unknown error')}")
    st.rerun()


def _manual_close(config, position_id: int) -> None:
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)
    result = mgr.manual_close(position_id)
    mgr.close()
    if result["status"] == "ok":
        st.toast("Position closed")
    else:
        st.error(f"Close failed: {result.get('message', 'unknown error')}")
    st.rerun()


def _approve_order(config, order) -> None:
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)
    errors = mgr.validate_for_approval(order)
    if errors:
        for err in errors:
            st.error(err, icon="⛔")
        mgr.close()
        return
    with st.spinner("Connecting and placing bracket order…"):
        result = mgr.approve_and_place(order)
    mgr.close()
    if result["status"] == "ok":
        st.toast(f"Bracket submitted — position #{result['position_id']}")
    else:
        st.error(f"Order failed: {result.get('message', 'unknown')}", icon="💥")
    st.rerun()


def _reject_order(config, order) -> None:
    from drift.storage.pending_order_store import PendingOrderStore

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    s = PendingOrderStore(db_path)
    s.set_state(order.id, "REJECTED", reject_reason="Operator rejected")
    s.close()
    st.rerun()


def _assess_position(config, pos) -> None:
    try:
        from drift.ai.position_advisor import assess_position
        with st.spinner("Getting LLM assessment…"):
            advice = assess_position(config, pos)
        st.info(advice, icon="🧠")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Quick-Assess failed: {exc}")
