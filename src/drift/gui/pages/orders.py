"""Orders — pending trade approvals for Interactive Brokers.

When a trade plan is issued and broker integration is enabled, a pending order
card appears here for operator approval.  Approving triggers a bracket order
submission to IB Gateway (paper or live).  Rejecting discards the order.

The page auto-refreshes every 10 seconds so approval cards appear promptly
without manual refresh.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import streamlit as st

from drift.gui.state import get_config, _PROJECT_ROOT

log = logging.getLogger(__name__)

_STATE_BADGE = {
    "PENDING":   ("⏳", "orange"),
    "APPROVED":  ("✅", "green"),
    "SUBMITTED": ("📤", "blue"),
    "REJECTED":  ("❌", "red"),
    "EXPIRED":   ("⌛", "grey"),
    "FAILED":    ("💥", "red"),
}

_BIAS_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}


@st.cache_resource
def _load_config():
    return get_config()


def _get_pending_store(config):
    """Return a PendingOrderStore for the live DB, or None if broker disabled."""
    if not config.broker.enabled:
        return None
    from drift.storage.pending_order_store import PendingOrderStore
    db_path = _PROJECT_ROOT / config.storage.sqlite_path
    return PendingOrderStore(str(db_path))


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


def _submit_order(config, order_row) -> None:
    """Call IBClient.submit_bracket and update the pending order state."""
    from drift.brokers.ib_client import IBClient
    from drift.storage.pending_order_store import PendingOrderStore

    store = PendingOrderStore(str(_PROJECT_ROOT / config.storage.sqlite_path))
    store.set_state(order_row.id, "APPROVED")

    client = IBClient(config.broker)
    with st.spinner("Connecting to IB Gateway and placing bracket order…"):
        result = client.submit_bracket(order_row)

    if result["status"] == "ok":
        store.set_state(
            order_row.id,
            "SUBMITTED",
            ib_order_id=result["order_id"],
            ib_perm_id=result["perm_id"],
        )
        st.success(
            f"Bracket order submitted — IB orderId **{result['order_id']}**",
            icon="📤",
        )
    else:
        store.set_state(
            order_row.id,
            "FAILED",
            reject_reason=result["message"],
        )
        st.error(f"IB order failed: {result['message']}", icon="💥")

    store.close()
    st.rerun()


def page() -> None:
    st.title("🏦 Orders")

    config = _load_config()

    if not config.broker.enabled:
        st.info(
            "Broker integration is disabled.  "
            "Set `broker.enabled: true` and configure your IB account in `config/settings.yaml`.",
            icon="ℹ️",
        )
        return

    store = _get_pending_store(config)
    if store is None:
        st.error("Could not open pending order store.")
        return

    # Expire stale pending orders before rendering
    expired = store.expire_stale(config.broker.approval_expiry_minutes)
    if expired:
        st.toast(f"{expired} pending order(s) expired (>{config.broker.approval_expiry_minutes} min old)")

    pending = store.get_pending()
    all_orders = store.get_all(limit=50)

    # Auto-refresh when there are pending approvals
    if pending:
        st.info(
            f"**{len(pending)} trade plan(s) awaiting your approval.** "
            "This page auto-refreshes every 10 seconds.",
            icon="⏳",
        )
        st.markdown(
            '<meta http-equiv="refresh" content="10">',
            unsafe_allow_html=True,
        )

    # ------------------------------------------------------------------
    # Pending approval cards
    # ------------------------------------------------------------------
    if pending:
        st.subheader("Pending Approvals")
        for order in pending:
            bias_emoji = _BIAS_EMOJI.get(order.bias, "")
            with st.container(border=True):
                col_title, col_age = st.columns([4, 1])
                col_title.markdown(
                    f"### {bias_emoji} {order.bias} {order.symbol}  "
                    f"`{order.setup_type}`  conf={order.confidence}%"
                )
                col_age.caption(_age_label(order.created_at))

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
                    f"  |  1 MNQ contract  |  Bracket order (entry + TP1 + SL)"
                )

                col_approve, col_reject, _ = st.columns([1, 1, 4])
                if col_approve.button(
                    "✅ Approve & Place", key=f"approve_{order.id}", type="primary"
                ):
                    _submit_order(config, order)

                if col_reject.button(
                    "❌ Reject", key=f"reject_{order.id}"
                ):
                    from drift.storage.pending_order_store import PendingOrderStore
                    s = PendingOrderStore(str(_PROJECT_ROOT / config.storage.sqlite_path))
                    s.set_state(order.id, "REJECTED", reject_reason="Operator rejected")
                    s.close()
                    st.rerun()

        st.divider()

    # ------------------------------------------------------------------
    # Order history
    # ------------------------------------------------------------------
    st.subheader("Order History")

    if not all_orders:
        st.caption("No orders yet.")
        store.close()
        return

    for order in all_orders:
        icon, _color = _STATE_BADGE.get(order.state, ("❓", "grey"))
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

    store.close()
