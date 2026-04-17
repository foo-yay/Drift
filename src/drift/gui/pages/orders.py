"""Orders — trade approval, active position management, and order history.

Sections:
    1. **Pending Approvals** — trade plans awaiting operator action with
       time/price validation and Approve / Reject buttons.
    2. **Active Positions** — filled entries with TP1/TP2/Manual exit toggles,
       Quick-Assess LLM button, and Manual Close.
    3. **Order History** — all past orders with expandable detail.

The page auto-refreshes every 10 seconds when orders or positions are active.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import streamlit as st

from drift.gui.state import get_config, _PROJECT_ROOT

log = logging.getLogger(__name__)

_STATE_BADGE = {
    "PENDING":        ("⏳", "orange"),
    "APPROVED":       ("✅", "green"),
    "SUBMITTED":      ("📤", "blue"),
    "REJECTED":       ("❌", "red"),
    "EXPIRED":        ("⌛", "grey"),
    "FAILED":         ("💥", "red"),
    "WORKING":        ("⏳", "orange"),
    "FILLED":         ("📊", "green"),
    "CLOSED_TP1":     ("🎯", "green"),
    "CLOSED_TP2":     ("🎯🎯", "green"),
    "CLOSED_SL":      ("🛑", "red"),
    "CLOSED_MANUAL":  ("🚪", "blue"),
    "CLOSED_CANCEL":  ("🚫", "grey"),
}

_BIAS_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}
_MODE_LABEL = {"TP1": "🎯 TP1", "TP2": "🎯🎯 TP2", "MANUAL": "✋ Manual"}


@st.cache_resource
def _load_config():
    return get_config()


def _age_label(iso_str: str) -> str:
    try:
        created = datetime.fromisoformat(iso_str).replace(tzinfo=timezone.utc)
        delta = datetime.now(tz=timezone.utc) - created
        mins = int(delta.total_seconds() / 60)
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{mins}m ago"
        return f"{mins // 60}h {mins % 60}m ago"
    except Exception:
        return iso_str


# ------------------------------------------------------------------
# Approval flow
# ------------------------------------------------------------------

def _submit_order(config, order_row) -> None:
    """Validate, place bracket, and create active position."""
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)

    # Validation
    errors = mgr.validate_for_approval(order_row)
    if errors:
        for err in errors:
            st.error(err, icon="⛔")
        mgr.close()
        return

    # Price validity warning (non-blocking)
    try:
        from drift.data.providers.yfinance_provider import YFinanceProvider
        provider = YFinanceProvider()
        current_price = provider.get_latest_quote(order_row.symbol)
        warnings = mgr.check_price_validity(order_row, current_price)
        for w in warnings:
            st.warning(w, icon="⚠️")
    except Exception:  # noqa: BLE001
        pass

    with st.spinner("Connecting to IB Gateway and placing bracket order…"):
        result = mgr.approve_and_place(order_row)

    mgr.close()

    if result["status"] == "ok":
        st.success(
            f"Bracket order submitted — position #{result['position_id']}  "
            f"IB orderId **{result['order_id']}**",
            icon="📤",
        )
    else:
        st.error(f"Order failed: {result.get('message', 'unknown')}", icon="💥")

    st.rerun()


# ------------------------------------------------------------------
# Exit mode switching
# ------------------------------------------------------------------

def _switch_exit_mode(config, position_id: int, mode: str) -> None:
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)
    with st.spinner(f"Switching to {mode}…"):
        result = mgr.switch_exit_mode(position_id, mode)
    mgr.close()

    if result["status"] == "ok":
        st.toast(f"Exit mode → {mode}")
    else:
        st.error(f"Switch failed: {result.get('message')}")
    st.rerun()


def _manual_close(config, position_id: int) -> None:
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)
    with st.spinner("Closing position at market…"):
        result = mgr.manual_close(position_id)
    mgr.close()

    if result["status"] == "ok":
        fill = result.get("fill_price")
        st.success(f"Position closed{f' at {fill:.2f}' if fill else ''}", icon="🚪")
    else:
        st.error(f"Close failed: {result.get('message')}")
    st.rerun()


# ------------------------------------------------------------------
# Quick-Assess LLM
# ------------------------------------------------------------------

def _quick_assess(config, pos) -> None:
    """Fire a quick LLM advisory query for the given position."""
    try:
        from drift.ai.position_advisor import assess_position

        with st.spinner("Getting LLM assessment…"):
            advice = assess_position(config, pos)
        st.info(advice, icon="🧠")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Quick-Assess failed: {exc}")


# ------------------------------------------------------------------
# Page
# ------------------------------------------------------------------

def page() -> None:
    st.title("🏦 Orders & Positions")

    config = _load_config()

    if not config.broker.enabled:
        st.info(
            "Broker integration is disabled.  "
            "Set `broker.enabled: true` in `config/settings.yaml`.",
            icon="ℹ️",
        )
        return

    # Load stores
    from drift.storage.pending_order_store import PendingOrderStore
    from drift.storage.position_store import PositionStore

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    pending_store = PendingOrderStore(db_path)
    position_store = PositionStore(db_path)

    # Expire stale pending orders
    expired = pending_store.expire_stale(config.broker.approval_expiry_minutes)
    if expired:
        st.toast(f"{expired} pending order(s) expired")

    pending = pending_store.get_pending()
    open_positions = position_store.get_open()
    needs_refresh = bool(pending) or bool(open_positions)

    # Auto-refresh
    if needs_refresh:
        st.markdown(
            '<meta http-equiv="refresh" content="10">',
            unsafe_allow_html=True,
        )

    # ==================================================================
    # Section 1: Active Positions
    # ==================================================================
    if open_positions:
        st.subheader("📊 Active Positions")
        for pos in open_positions:
            _render_active_position(config, pos)
        st.divider()

    # ==================================================================
    # Section 2: Pending Approvals
    # ==================================================================
    if pending:
        st.subheader("⏳ Pending Approvals")
        st.caption(f"{len(pending)} trade plan(s) awaiting approval")
        for order in pending:
            _render_pending_card(config, order)
        st.divider()

    # ==================================================================
    # Section 3: IB Status
    # ==================================================================
    with st.expander("🔌 IB Gateway Status", expanded=False):
        if st.button("Test Connection"):
            from drift.brokers.ib_client import IBClient
            client = IBClient(config.broker)
            with st.spinner("Testing…"):
                result = client.check_connectivity()
            if result["status"] == "ok":
                st.success("Connected to IB Gateway", icon="✅")
            else:
                st.error(f"Connection failed: {result.get('message')}", icon="❌")

    # ==================================================================
    # Section 4: History
    # ==================================================================
    st.subheader("📋 Order & Position History")

    all_orders = pending_store.get_all(limit=50)
    all_positions = position_store.get_all(limit=50)

    tab_orders, tab_positions = st.tabs(["Orders", "Positions"])

    with tab_orders:
        if not all_orders:
            st.caption("No orders yet.")
        else:
            for order in all_orders:
                _render_order_history_row(order)

    with tab_positions:
        if not all_positions:
            st.caption("No positions yet.")
        else:
            for pos in all_positions:
                _render_position_history_row(pos)

    pending_store.close()
    position_store.close()


# ------------------------------------------------------------------
# Renderers
# ------------------------------------------------------------------

def _render_pending_card(config, order) -> None:
    """Render a pending approval card with validation warnings."""
    bias_emoji = _BIAS_EMOJI.get(order.bias, "")

    # Pre-compute validation warnings
    time_warning = ""
    if order.generated_at:
        try:
            gen = datetime.fromisoformat(order.generated_at)
            if gen.tzinfo is None:
                gen = gen.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(tz=timezone.utc) - gen).total_seconds() / 60
            if elapsed > order.max_hold_minutes:
                time_warning = f"⚠️ EXPIRED — {elapsed:.0f}m old (max {order.max_hold_minutes}m)"
            elif elapsed > order.max_hold_minutes * 0.7:
                time_warning = f"⏰ {order.max_hold_minutes - elapsed:.0f}m remaining"
        except (ValueError, TypeError):
            pass

    with st.container(border=True):
        col_title, col_age = st.columns([4, 1])
        col_title.markdown(
            f"### {bias_emoji} {order.bias} {order.symbol}  "
            f"`{order.setup_type}`  conf={order.confidence}%"
        )
        col_age.caption(_age_label(order.created_at))

        if time_warning:
            st.warning(time_warning)

        st.caption(order.thesis)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Entry", f"{order.entry_min:.2f} – {order.entry_max:.2f}")
        c2.metric("Stop Loss", f"{order.stop_loss:.2f}")
        c3.metric("TP1", f"{order.take_profit_1:.2f}")
        c4.metric(
            "TP2",
            f"{order.take_profit_2:.2f}" if order.take_profit_2 else "—",
        )

        st.caption(
            f"Entry limit: **{'entry_max' if order.bias == 'LONG' else 'entry_min'}** "
            f"({order.entry_max if order.bias == 'LONG' else order.entry_min:.2f})"
            f"  |  1 MNQ  |  Hold: {order.max_hold_minutes}m  |  Bracket (entry + TP1 + SL)"
        )

        col_approve, col_reject, _ = st.columns([1, 1, 4])
        if col_approve.button(
            "✅ Approve & Place", key=f"approve_{order.id}", type="primary"
        ):
            _submit_order(config, order)

        if col_reject.button("❌ Reject", key=f"reject_{order.id}"):
            from drift.storage.pending_order_store import PendingOrderStore
            s = PendingOrderStore(str(_PROJECT_ROOT / config.storage.sqlite_path))
            s.set_state(order.id, "REJECTED", reject_reason="Operator rejected")
            s.close()
            st.rerun()


def _render_active_position(config, pos) -> None:
    """Render an active position card with exit mode controls."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")
    mode_label = _MODE_LABEL.get(pos.exit_mode, pos.exit_mode)

    with st.container(border=True):
        # Title row
        state_label = "⏳ Entry pending" if pos.state == "WORKING" else "📊 FILLED"
        st.markdown(
            f"### {bias_emoji} {pos.bias} {pos.symbol}  —  {state_label}  |  {mode_label}"
        )

        # Metrics row
        c1, c2, c3, c4, c5 = st.columns(5)
        if pos.entry_fill:
            c1.metric("Entry Fill", f"{pos.entry_fill:.2f}")
        else:
            c1.metric("Entry Limit", f"{pos.entry_limit:.2f}")
        c2.metric("Stop Loss", f"{pos.stop_loss:.2f}")
        c3.metric("TP1", f"{pos.take_profit_1:.2f}")
        c4.metric("TP2", f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—")

        # P&L (for FILLED)
        if pos.entry_fill:
            try:
                from drift.data.providers.yfinance_provider import YFinanceProvider
                current = YFinanceProvider().get_latest_quote(pos.symbol)
                pts = (current - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current)
                usd = pts * 0.50 * pos.quantity
                c5.metric("P&L", f"{pts:+.2f} pts", f"${usd:+.2f}")
            except Exception:  # noqa: BLE001
                c5.metric("P&L", "—")

        # Time info
        if pos.fill_time:
            try:
                fill_dt = datetime.fromisoformat(pos.fill_time)
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(tz=timezone.utc) - fill_dt).total_seconds() / 60
                remaining = pos.max_hold_minutes - elapsed
                if remaining > 0:
                    st.caption(f"Filled {_age_label(pos.fill_time)}  •  {remaining:.0f}m remaining in hold window")
                else:
                    st.caption(f"Filled {_age_label(pos.fill_time)}  •  ⚠️ Hold window expired ({abs(remaining):.0f}m ago)")
            except (ValueError, TypeError):
                pass

        st.caption(pos.thesis)

        # Action buttons (only for FILLED)
        if pos.state == "FILLED":
            cols = st.columns([1, 1, 1, 1, 1, 3])

            if pos.exit_mode != "TP1" and pos.take_profit_1:
                if cols[0].button("🎯 TP1", key=f"ord_tp1_{pos.id}"):
                    _switch_exit_mode(config, pos.id, "TP1")

            if pos.exit_mode != "TP2" and pos.take_profit_2:
                if cols[1].button("🎯🎯 TP2", key=f"ord_tp2_{pos.id}"):
                    _switch_exit_mode(config, pos.id, "TP2")

            if pos.exit_mode != "MANUAL":
                if cols[2].button("✋ Hold", key=f"ord_hold_{pos.id}"):
                    _switch_exit_mode(config, pos.id, "MANUAL")

            if cols[3].button("🚪 Close", key=f"ord_close_{pos.id}", type="primary"):
                _manual_close(config, pos.id)

            if cols[4].button("🧠 Assess", key=f"ord_assess_{pos.id}"):
                _quick_assess(config, pos)

        elif pos.state == "WORKING":
            if st.button("🚫 Cancel Order", key=f"ord_cancel_{pos.id}"):
                _manual_close(config, pos.id)  # cancel_bracket for WORKING


def _render_order_history_row(order) -> None:
    icon, _ = _STATE_BADGE.get(order.state, ("❓", "grey"))
    bias_emoji = _BIAS_EMOJI.get(order.bias, "")
    with st.expander(
        f"{icon} {order.state}  —  {bias_emoji} {order.bias} {order.symbol}  "
        f"`{order.setup_type}`  {_age_label(order.created_at)}",
        expanded=False,
    ):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Entry", f"{order.entry_min:.2f} – {order.entry_max:.2f}")
        c2.metric("Stop", f"{order.stop_loss:.2f}")
        c3.metric("TP1", f"{order.take_profit_1:.2f}")
        c4.metric("Conf", f"{order.confidence}%")
        if order.ib_order_id:
            st.caption(f"IB orderId={order.ib_order_id}  permId={order.ib_perm_id}")
        if order.reject_reason:
            st.caption(f"Reason: {order.reject_reason}")


def _render_position_history_row(pos) -> None:
    icon, _ = _STATE_BADGE.get(pos.state, ("❓", "grey"))
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")

    pnl_str = ""
    if pos.entry_fill and pos.exit_price:
        pts = (pos.exit_price - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - pos.exit_price)
        usd = pts * 0.50 * pos.quantity
        pnl_str = f"  |  {pts:+.2f} pts (${usd:+.2f})"

    with st.expander(
        f"{icon} {pos.state}  —  {bias_emoji} {pos.bias} {pos.symbol}  "
        f"`{pos.setup_type}`{pnl_str}  {_age_label(pos.created_at)}",
        expanded=False,
    ):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Entry", f"{pos.entry_fill or pos.entry_limit:.2f}")
        c2.metric("Exit", f"{pos.exit_price:.2f}" if pos.exit_price else "—")
        c3.metric("SL", f"{pos.stop_loss:.2f}")
        c4.metric("Mode", pos.exit_mode)
        if pos.exit_reason:
            st.caption(f"Exit: {pos.exit_reason}")
        if pos.parent_order_id:
            st.caption(f"IB parent={pos.parent_order_id}  tp={pos.tp_order_id}  sl={pos.sl_order_id}")
