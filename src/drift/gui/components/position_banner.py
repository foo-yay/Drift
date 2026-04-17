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
    "MANUAL":      "✋ Manual",
    "HOLD_EXPIRY": "⏰ Expiry",
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
        if remaining >= 0.5:
            return f"⏱ {remaining:.0f}m"
        if remaining > -0.5:
            return "⏱ 0m"
        over = abs(remaining)
        if pos.exit_mode == "MANUAL":
            return f"✋ +{over:.0f}m past window"
        return "⏰ closing..."
    except (ValueError, TypeError):
        return ""


@st.fragment(run_every=15)
def render_position_banner() -> None:
    """Render pending approvals + open positions at the top of every page."""
    config = get_config()
    if not config.broker.enabled:
        return

    try:
        from drift.storage.trade_store import TradeStore

        db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)

        store = TradeStore(db_path)
        store.expire_stale(config.broker.approval_expiry_minutes)
        all_open = store.get_open()
        store.close()
    except Exception:  # noqa: BLE001
        return

    pending_orders = [t for t in all_open if t.state == "PENDING"]
    active_positions = [t for t in all_open if t.state in ("WORKING", "FILLED")]

    if not active_positions and not pending_orders:
        return

    for order in pending_orders:
        _render_pending_banner_card(config, order)

    for pos in active_positions:
        _render_position_card(config, pos)


def _render_pending_banner_card(config, order) -> None:
    """Compact pending-approval card: info block + full-width button row."""
    bias_emoji = _BIAS_EMOJI.get(order.bias, "")

    time_str = ""
    if order.generated_at:
        try:
            gen = datetime.fromisoformat(order.generated_at)
            if gen.tzinfo is None:
                gen = gen.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(tz=timezone.utc) - gen).total_seconds() / 60
            remaining = order.max_hold_minutes - elapsed
            if remaining >= 0.5:
                time_str = f"⏱ {remaining:.0f}m"
            elif remaining > -0.5:
                time_str = "⏱ 0m"
            else:
                time_str = f"⚠️ +{abs(remaining):.0f}m past window"
        except (ValueError, TypeError):
            pass

    tp2_str = f"{order.take_profit_2:.2f}" if order.take_profit_2 else "—"
    entry_str = f"{order.entry_min:.2f}–{order.entry_max:.2f}"
    time_html = f" <span style='color:#888;font-size:0.85em'>{time_str}</span>" if time_str else ""

    info_html = (
        f"<div style='line-height:1.3;font-size:0.88rem;margin-bottom:10px'>"
        f"⏳ {bias_emoji} <strong>{order.bias} {order.symbol}</strong>"
        f" · <code>{order.setup_type}</code> · {order.confidence}%"
        f" &ensp;Entry <strong>{entry_str}</strong>"
        f" &ensp;<span style='color:#e05252'>SL</span> {order.stop_loss:.2f}"
        f" &ensp;<span style='color:#52b788'>TP1</span> {order.take_profit_1:.2f}"
        f" &ensp;<span style='color:#52b788'>TP2</span> {tp2_str}"
        f"{time_html}</div>"
    )

    with st.container(border=True):
        st.markdown(info_html, unsafe_allow_html=True)
        with st.container(horizontal=True, horizontal_alignment="left", gap="small"):
            if st.button("✅ Approve", key=f"bn_approve_{order.id}", type="primary", width="content"):
                _approve_order(config, order)
            if st.button("🧠 Assess", key=f"bn_assess_pend_{order.id}", width="content"):
                st.info("LLM assessment for pending orders is not yet implemented.", icon="🧠")
            if st.button("❌ Reject", key=f"bn_reject_{order.id}", width="content"):
                _reject_order(config, order)


def _render_position_card(config, pos) -> None:
    """Compact position card: info block + full-width button row."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")
    mode_badge = _MODE_BADGE.get(pos.exit_mode, pos.exit_mode)
    tp2_str = f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—"

    # Entry display — filled shows fill price, working shows limit
    if pos.entry_fill:
        entry_part = f"filled @ <strong>{pos.entry_fill:.2f}</strong>"
    elif pos.entry_limit:
        entry_part = f"limit @ <strong>{pos.entry_limit:.2f}</strong>"
    else:
        entry_part = f"entry <strong>{pos.entry_min:.2f}–{pos.entry_max:.2f}</strong>"

    # Status label
    state_icon = "📊" if pos.state == "FILLED" else "⏳"
    state_tag = (
        f"<span style='color:#52b788'>filled</span>"
        if pos.state == "FILLED"
        else "<span style='color:#e8a838'>awaiting fill</span>"
    )

    # P&L (filled only)
    pnl_html = ""
    if pos.entry_fill:
        try:
            from drift.data.providers.yfinance_provider import YFinanceProvider
            current_price = YFinanceProvider().get_latest_quote(pos.symbol)
            pts = (current_price - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current_price)
            usd = pts * 0.50 * pos.quantity
            clr = "#52b788" if pts >= 0 else "#e05252"
            pnl_html = (
                f" &ensp; <span style='color:{clr};white-space:nowrap'>"
                f"{pts:+.2f} pts (${usd:+.2f})</span>"
            )
        except Exception:  # noqa: BLE001
            pass

    time_md = _time_display(pos)
    time_html = f" <span style='color:#888;font-size:0.85em'>{time_md}</span>" if time_md else ""

    info_html = (
        f"<div style='line-height:1.3;font-size:0.88rem;margin-bottom:10px'>"
        f"{state_icon} {bias_emoji} <strong>{pos.bias} {pos.symbol}</strong>"
        f" · {entry_part} · {mode_badge}{pnl_html}"
        f" &ensp;<span style='color:#e05252'>SL</span> {pos.stop_loss:.2f}"
        f" &ensp;<span style='color:#52b788'>TP1</span> {pos.take_profit_1:.2f}"
        f" &ensp;<span style='color:#52b788'>TP2</span> {tp2_str}"
        f" &ensp;{state_tag}{time_html}</div>"
    )

    # Build button list
    if pos.state == "FILLED":
        btn_keys: list[str] = []
        if pos.exit_mode != "TP1" and pos.take_profit_1:
            btn_keys.append("tp1")
        if pos.exit_mode != "TP2" and pos.take_profit_2:
            btn_keys.append("tp2")
        btn_keys += ["hold", "close", "assess"]
    else:
        btn_keys = ["cancel", "assess"]

    with st.container(border=True):
        st.markdown(info_html, unsafe_allow_html=True)

        with st.container(horizontal=True, horizontal_alignment="left", gap="small"):
            if pos.state == "FILLED":
                if "tp1" in btn_keys:
                    if st.button("🎯 TP1", key=f"bn_tp1_{pos.id}",
                                 help=f"Switch exit to TP1 @ {pos.take_profit_1:.2f}", width="content"):
                        _switch_mode(config, pos.id, "TP1")
                if "tp2" in btn_keys:
                    if st.button("🎯 TP2", key=f"bn_tp2_{pos.id}",
                                 help=f"Switch exit to TP2 @ {pos.take_profit_2:.2f}", width="content"):
                        _switch_mode(config, pos.id, "TP2")
                with st.popover("✋ Hold", width="content"):
                    st.markdown("**Choose hold mode**")
                    if st.button("✋ Hold indefinitely", key=f"bn_hold_indef_{pos.id}",
                                 disabled=(pos.exit_mode == "MANUAL"), width="content"):
                        _switch_mode(config, pos.id, "MANUAL")
                    if st.button("⏰ Hold to expiry", key=f"bn_hold_exp_{pos.id}",
                                 disabled=(pos.exit_mode == "HOLD_EXPIRY"), width="content"):
                        _switch_mode(config, pos.id, "HOLD_EXPIRY")
                if st.button("✕ Close", key=f"bn_close_{pos.id}",
                             help="Close at market", width="content"):
                    _manual_close(config, pos.id)
                if st.button("🧠 Assess", key=f"bn_assess_{pos.id}",
                             help="Quick AI assessment", width="content"):
                    _assess_position(config, pos)
            else:
                if st.button("🚫 Cancel", key=f"bn_cancel_{pos.id}",
                             help="Cancel working entry order", width="content"):
                    _manual_close(config, pos.id)
                if st.button("🧠 Assess", key=f"bn_assess_wk_{pos.id}",
                             help="Quick AI assessment", width="content"):
                    _assess_position(config, pos)


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
    from drift.storage.trade_store import TradeStore

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    s = TradeStore(db_path)
    s.set_state(order.id, "REJECTED")
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
