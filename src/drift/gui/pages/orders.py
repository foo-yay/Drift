"""Orders — trade approval, active position management, and order history.

Sections:
    1. **Active Positions** — filled entries with exit mode controls.
    2. **Pending Approvals** — trade plans awaiting operator action.
    3. **IB Status** — connectivity check.
    4. **Order History** — all past orders with expandable detail.

Auto-refresh uses st.fragment(run_every=15) so only the live sections
rerun — no full-page reload or scroll-position reset.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import streamlit as st

from drift.gui.state import get_config, _PROJECT_ROOT

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auto-refreshing live sections (fragment = WebSocket rerun, no page reload)
# ---------------------------------------------------------------------------

@st.fragment(run_every=15)
def _positions_section(config, db_path: str) -> None:
    """Active positions — reruns every 15 s to update P&L without a page reload."""
    from drift.storage.position_store import PositionStore

    store = PositionStore(db_path)
    open_positions = store.get_open()
    store.close()
    if not open_positions:
        return
    st.subheader("📊 Active Positions")
    for pos in open_positions:
        _render_active_position(config, pos)
    st.divider()


@st.fragment(run_every=15)
def _pending_section(config, db_path: str) -> None:
    """Pending approvals — reruns every 15 s so expiry is checked live."""
    from drift.storage.pending_order_store import PendingOrderStore

    store = PendingOrderStore(db_path)
    store.expire_stale(config.broker.approval_expiry_minutes)
    pending = store.get_pending()
    store.close()
    if not pending:
        return
    st.subheader("⏳ Pending Approvals")
    st.caption(f"{len(pending)} trade plan(s) awaiting approval")
    for order in pending:
        _render_pending_card(config, order)
    st.divider()


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

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)

    # ==================================================================
    # Section 1: Active Positions  (fragment — no full-page reload)
    # ==================================================================
    _positions_section(config, db_path)

    # ==================================================================
    # Section 2: Pending Approvals  (fragment — no full-page reload)
    # ==================================================================
    _pending_section(config, db_path)

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

    from drift.storage.pending_order_store import PendingOrderStore
    from drift.storage.position_store import PositionStore

    hist_pending = PendingOrderStore(db_path)
    hist_positions = PositionStore(db_path)
    all_orders = hist_pending.get_all(limit=50)
    all_positions = hist_positions.get_all(limit=50)

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

    hist_pending.close()
    hist_positions.close()


# ------------------------------------------------------------------
# Renderers
# ------------------------------------------------------------------

def _render_pending_card(config, order) -> None:
    """Render a compact pending approval row."""
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
        c0, c1, c2 = st.columns([3, 5, 2], vertical_alignment="top")
        c0.markdown(
            f"{bias_emoji} **{order.bias} {order.symbol}**  \n"
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
        c2.markdown(f"*{_age_label(order.created_at)}* · hold {order.max_hold_minutes}m")

        col_approve, col_reject, _ = st.columns([1, 1, 6])
        if col_approve.button("✅ Approve", key=f"approve_{order.id}", type="primary"):
            _submit_order(config, order)
        if col_reject.button("❌ Reject", key=f"reject_{order.id}"):
            from drift.storage.pending_order_store import PendingOrderStore
            s = PendingOrderStore(str(_PROJECT_ROOT / config.storage.sqlite_path))
            s.set_state(order.id, "REJECTED", reject_reason="Operator rejected")
            s.close()
            st.rerun()


def _render_active_position(config, pos) -> None:
    """Render a compact active position row with exit controls."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")
    state_label = "⏳ pending" if pos.state == "WORKING" else "📊 filled"
    mode_label = _MODE_LABEL.get(pos.exit_mode, pos.exit_mode)
    entry_str = f"{pos.entry_fill:.2f}" if pos.entry_fill else f"lim {pos.entry_limit:.2f}"
    tp2_str = f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—"

    # P&L
    pnl_str = ""
    if pos.entry_fill:
        try:
            from drift.data.providers.yfinance_provider import YFinanceProvider
            current = YFinanceProvider().get_latest_quote(pos.symbol)
            pts = (current - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current)
            usd = pts * 0.50 * pos.quantity
            color = "green" if pts >= 0 else "red"
            pnl_str = f":{color}[{pts:+.2f} pts (${usd:+.2f})]"
        except Exception:  # noqa: BLE001
            pnl_str = "P&L —"

    # Time display (reuse same logic as banner)
    time_str = ""
    if pos.fill_time and pos.max_hold_minutes:
        try:
            fill_dt = datetime.fromisoformat(pos.fill_time)
            if fill_dt.tzinfo is None:
                fill_dt = fill_dt.replace(tzinfo=timezone.utc)
            remaining = pos.max_hold_minutes - (datetime.now(tz=timezone.utc) - fill_dt).total_seconds() / 60
            if remaining > 0:
                time_str = f"⏱ {remaining:.0f}m"
            elif pos.exit_mode == "MANUAL":
                time_str = f"✋ +{abs(remaining):.0f}m past window"
            else:
                time_str = f"⚠️ +{abs(remaining):.0f}m past window"
        except (ValueError, TypeError):
            pass

    # Determine buttons before column layout so widths are correct
    if pos.state == "FILLED":
        btn_labels: list[str] = []
        if pos.exit_mode != "TP1" and pos.take_profit_1:
            btn_labels.append("tp1")
        if pos.exit_mode != "TP2" and pos.take_profit_2:
            btn_labels.append("tp2")
        if pos.exit_mode != "MANUAL":
            btn_labels.append("hold")
        btn_labels += ["close", "assess"]
    elif pos.state == "WORKING":
        btn_labels = ["cancel"]
    else:
        btn_labels = []

    col_widths = [2, 3.5, 1.5] + [1.1] * len(btn_labels)

    with st.container(border=True):
        cols = st.columns(col_widths, vertical_alignment="top")
        c0, c1, c2 = cols[0], cols[1], cols[2]
        btn_cols = cols[3:]

        c0.markdown(
            f"{bias_emoji} **{pos.bias} {pos.symbol}**  \n"
            f"<small style='color:#aaa'>{state_label} · {mode_label}</small>",
            unsafe_allow_html=True,
        )
        c1.markdown(
            f"<small style='color:#aaa'>Entry</small> **{entry_str}** &ensp;"
            f"<small style='color:#e05252'>SL</small> **{pos.stop_loss:.2f}**<br>"
            f"<small style='color:#52b788'>TP1</small> **{pos.take_profit_1:.2f}** &ensp;"
            f"<small style='color:#52b788'>TP2</small> **{tp2_str}**",
            unsafe_allow_html=True,
        )
        right_parts = []
        if pnl_str:
            right_parts.append(pnl_str)
        if time_str:
            right_parts.append(f"<small>{time_str}</small>")
        if right_parts:
            c2.markdown("  \n".join(right_parts), unsafe_allow_html=True)

        # Buttons — inline right side, top-aligned
        i = 0
        if pos.state == "FILLED":
            if "tp1" in btn_labels:
                if btn_cols[i].button("→TP1", key=f"ord_tp1_{pos.id}",
                                      help=f"Switch exit to TP1 @ {pos.take_profit_1:.2f}"):
                    _switch_exit_mode(config, pos.id, "TP1")
                i += 1
            if "tp2" in btn_labels:
                if btn_cols[i].button("→TP2", key=f"ord_tp2_{pos.id}",
                                      help=f"Switch exit to TP2 @ {pos.take_profit_2:.2f}"):
                    _switch_exit_mode(config, pos.id, "TP2")
                i += 1
            if "hold" in btn_labels:
                if btn_cols[i].button("✋ Hold", key=f"ord_hold_{pos.id}",
                                      help="Hold manually — disarms auto-exit. Position stays open past time window until you close it or SL/TP triggers."):
                    _switch_exit_mode(config, pos.id, "MANUAL")
                i += 1
            if btn_cols[i].button("✕ Close", key=f"ord_close_{pos.id}",
                                  help="Submit market order to close immediately"):
                _manual_close(config, pos.id)
            i += 1
            if btn_cols[i].button("🧠 Assess", key=f"ord_assess_{pos.id}"):
                _quick_assess(config, pos)
        elif pos.state == "WORKING" and btn_cols:
            if btn_cols[0].button("🚫 Cancel Order", key=f"ord_cancel_{pos.id}"):
                _manual_close(config, pos.id)


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
