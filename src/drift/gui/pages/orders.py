"""Orders — trade approval, active position management, and order history.

Sections:
    1. **Active Positions** — filled entries with exit mode controls.
    2. **Pending Approvals** — trade plans awaiting operator action.
    3. **IB Status** — connectivity check.
    4. **Order History** — all past orders with expandable detail.

Auto-refresh uses st.fragment(run_every=10) so only the live sections
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

@st.fragment(run_every=10)
def _positions_section(config, db_path: str) -> None:
    """Active positions — reruns every 10 s to update P&L without a page reload."""
    from drift.storage.trade_store import TradeStore

    store = TradeStore(db_path)
    active = store.get_active()
    store.close()
    if not active:
        return
    st.subheader("📊 Active Positions")
    for pos in active:
        _render_active_position(config, pos)
    st.divider()


@st.fragment(run_every=10)
def _pending_section(config, db_path: str) -> None:
    """Pending approvals — reruns every 10 s so expiry is checked live."""
    from drift.storage.trade_store import TradeStore

    store = TradeStore(db_path)
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
_MODE_LABEL = {
    "TP1":         "🎯 TP1",
    "TP2":         "🎯🎯 TP2",
    "MANUAL":      "✋ Hold (indefinite)",
    "HOLD_EXPIRY": "⏰ Hold to expiry",
}


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
    from drift.gui.state import get_live_price
    current_price = get_live_price(order_row.symbol)
    if current_price is not None:
        warnings = mgr.check_price_validity(order_row, current_price)
        for w in warnings:
            st.warning(w, icon="⚠️")

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
# Structured Assess
# ------------------------------------------------------------------

def _quick_assess(config, pos) -> None:
    """Run a structured LLM assessment and show recommendations."""
    try:
        from drift.ai.position_advisor import assess_position
        from drift.brokers.position_manager import PositionManager

        db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)

        with st.spinner("Getting LLM assessment…"):
            rec = assess_position(config, pos)

        mgr = PositionManager(config, db_path)
        assess_id = mgr.log_assessment(pos.id, rec)
        mgr.close()

        # Store in session state so it survives fragment reruns
        st.session_state[f"ord_assess_result_{pos.id}"] = {
            "rec": rec,
            "assess_id": assess_id,
        }

    except Exception as exc:  # noqa: BLE001
        st.error(f"Assessment failed: {exc}")


def _show_stored_assessment(config, pos) -> None:
    """Render a persisted assessment from session state if one exists."""
    key = f"ord_assess_result_{pos.id}"
    data = st.session_state.get(key)
    if not data:
        return
    _render_assessment(config, pos, data["rec"], data["assess_id"])


def _render_assessment(config, pos, rec, assess_id: int) -> None:
    """Display a structured assessment with Apply/Dismiss actions."""
    action_colors = {"HOLD": "#52b788", "ADJUST": "#e8a838", "CLOSE": "#e05252"}
    color = action_colors.get(rec.action, "#888")

    changes: list[str] = []
    if rec.new_stop_loss is not None:
        changes.append(f"SL → {rec.new_stop_loss:.2f}")
    if rec.new_take_profit_1 is not None:
        changes.append(f"TP1 → {rec.new_take_profit_1:.2f}")
    if rec.new_take_profit_2 is not None:
        changes.append(f"TP2 → {rec.new_take_profit_2:.2f}")
    if rec.new_entry_limit is not None:
        changes.append(f"Entry → {rec.new_entry_limit:.2f}")
    if rec.new_max_hold_minutes is not None:
        changes.append(f"Hold → {rec.new_max_hold_minutes}m")
    if rec.recommended_exit_mode:
        changes.append(f"Mode → {rec.recommended_exit_mode}")
    changes_html = " · ".join(changes) if changes else "No parameter changes"

    flags_html = ""
    if rec.risk_flags:
        flags_html = (
            f"<div style='color:#e8a838;font-size:0.82rem;margin-top:4px'>"
            f"⚠️ {' · '.join(rec.risk_flags)}</div>"
        )

    st.markdown(
        f"<div style='border-left:3px solid {color};padding-left:8px;margin:6px 0'>"
        f"<div style='font-size:0.9rem'>"
        f"<strong style='color:{color}'>{rec.action}</strong>"
        f" · {rec.confidence}% confidence</div>"
        f"<div style='font-size:0.85rem;color:#ccc;margin-top:2px'>{rec.rationale}</div>"
        f"<div style='font-size:0.82rem;margin-top:4px'>{changes_html}</div>"
        f"{flags_html}</div>",
        unsafe_allow_html=True,
    )

    if rec.action in ("ADJUST", "CLOSE"):
        with st.container(horizontal=True, horizontal_alignment="left", gap="small"):
            if st.button(
                "✅ Apply", key=f"ord_apply_assess_{pos.id}_{assess_id}",
                type="primary", width="content",
            ):
                _apply_assessment(config, pos.id, rec, assess_id)
            if st.button(
                "✕ Dismiss", key=f"ord_dismiss_assess_{pos.id}_{assess_id}",
                width="content",
            ):
                _dismiss_assessment(config, assess_id, position_id=pos.id)
    else:
        # HOLD — show a clear button so the assessment can be dismissed
        if st.button("✕ Clear", key=f"ord_clear_assess_{pos.id}_{assess_id}", width="content"):
            _dismiss_assessment(config, assess_id, position_id=pos.id)


def _apply_assessment(config, position_id: int, rec, assess_id: int) -> None:
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)
    with st.spinner("Applying assessment changes…"):
        result = mgr.apply_assessment(position_id, rec)
    if result["status"] == "ok":
        mgr.mark_assessment_applied(assess_id)
        applied_changes = result.get("changes", [])
        st.toast(f"Applied: {', '.join(applied_changes)}")
    else:
        st.error(f"Failed: {result.get('message', 'unknown error')}")
    mgr.close()
    st.session_state.pop(f"ord_assess_result_{position_id}", None)
    st.rerun()


def _dismiss_assessment(config, assess_id: int, position_id: int = 0) -> None:
    from drift.brokers.position_manager import PositionManager

    db_path = str(_PROJECT_ROOT / config.storage.sqlite_path)
    mgr = PositionManager(config, db_path)
    mgr.dismiss_assessment(assess_id)
    mgr.close()
    st.session_state.pop(f"ord_assess_result_{position_id}", None)
    st.toast("Assessment dismissed")
    st.rerun()


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
            client = IBClient(config.broker, config.instrument)
            with st.spinner("Testing…"):
                result = client.check_connectivity()
            if result["status"] == "ok":
                st.success("Connected to IB Gateway", icon="✅")
            else:
                st.error(f"Connection failed: {result.get('message')}", icon="❌")

    # ==================================================================
    # Section 4: History
    # ==================================================================
    st.subheader("📋 Trade History")

    from drift.storage.trade_store import TradeStore

    hist_store = TradeStore(db_path)
    history = hist_store.get_history(limit=50)
    hist_store.close()

    if not history:
        st.caption("No completed trades yet.")
    else:
        for trade in history:
            _render_trade_history_row(trade)


# ------------------------------------------------------------------
# Renderers
# ------------------------------------------------------------------


def _render_pending_card(config, order) -> None:
    """Compact pending approval card: info block + full-width button row."""
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
            if st.button("✅ Approve", key=f"approve_{order.id}", type="primary", width="content"):
                _submit_order(config, order)
            if st.button("🧠 Assess", key=f"assess_pend_{order.id}", width="content"):
                _quick_assess(config, order)
            if st.button("❌ Reject", key=f"reject_{order.id}", width="content"):
                from drift.storage.trade_store import TradeStore
                s = TradeStore(str(_PROJECT_ROOT / config.storage.sqlite_path))
                s.set_state(order.id, "REJECTED", reject_reason="Operator rejected")
                s.close()
                st.rerun()

        # Show persisted assessment if one exists
        _show_stored_assessment(config, order)


def _render_active_position(config, pos) -> None:
    """Compact active position card: info block + full-width button row."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")
    mode_label = _MODE_LABEL.get(pos.exit_mode, pos.exit_mode)
    tp2_str = f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—"

    # Entry display
    if pos.entry_fill:
        entry_part = f"filled @ <strong>{pos.entry_fill:.2f}</strong>"
    elif pos.entry_limit:
        entry_part = f"limit @ <strong>{pos.entry_limit:.2f}</strong>"
    else:
        entry_part = f"entry <strong>{pos.entry_min:.2f}–{pos.entry_max:.2f}</strong>"

    # Status
    state_icon = "📊" if pos.state == "FILLED" else "⏳"
    state_tag = (
        "<span style='color:#52b788'>filled</span>"
        if pos.state == "FILLED"
        else "<span style='color:#e8a838'>awaiting fill</span>"
    )

    # P&L (filled only)
    pnl_html = ""
    if pos.entry_fill:
        from drift.gui.state import get_live_price, get_tick_value
        current = get_live_price(pos.symbol)
        if current is not None:
            pts = (current - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current)
            usd = pts * get_tick_value(pos.symbol, config) * pos.quantity
            clr = "#52b788" if pts >= 0 else "#e05252"
            pnl_html = (
                f" &ensp; <span style='color:{clr};white-space:nowrap'>"
                f"{pts:+.2f} pts (${usd:+.2f})</span>"
            )

    # Time display
    time_str = ""
    if pos.fill_time and pos.max_hold_minutes:
        try:
            fill_dt = datetime.fromisoformat(pos.fill_time)
            if fill_dt.tzinfo is None:
                fill_dt = fill_dt.replace(tzinfo=timezone.utc)
            remaining = pos.max_hold_minutes - (datetime.now(tz=timezone.utc) - fill_dt).total_seconds() / 60
            if remaining >= 0.5:
                time_str = f"⏱ {remaining:.0f}m"
            elif remaining > -0.5:
                time_str = "⏱ 0m"
            elif pos.exit_mode == "MANUAL":
                time_str = f"✋ +{abs(remaining):.0f}m past window"
            else:
                time_str = "⏰ closing..."
        except (ValueError, TypeError):
            pass

    time_html = f" <span style='color:#888;font-size:0.85em'>{time_str}</span>" if time_str else ""

    info_html = (
        f"<div style='line-height:1.3;font-size:0.88rem;margin-bottom:10px'>"
        f"{state_icon} {bias_emoji} <strong>{pos.bias} {pos.symbol}</strong>"
        f" · {entry_part} · {mode_label}{pnl_html}"
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
    elif pos.state == "WORKING":
        btn_keys = ["cancel", "assess"]
    else:
        btn_keys = []

    with st.container(border=True):
        st.markdown(info_html, unsafe_allow_html=True)

        if not btn_keys:
            return

        with st.container(horizontal=True, horizontal_alignment="left", gap="small"):
            if pos.state == "FILLED":
                if "tp1" in btn_keys:
                    if st.button("🎯 TP1", key=f"ord_tp1_{pos.id}",
                                 help=f"Switch exit to TP1 @ {pos.take_profit_1:.2f}", width="content"):
                        _switch_exit_mode(config, pos.id, "TP1")
                if "tp2" in btn_keys:
                    if st.button("🎯 TP2", key=f"ord_tp2_{pos.id}",
                                 help=f"Switch exit to TP2 @ {pos.take_profit_2:.2f}", width="content"):
                        _switch_exit_mode(config, pos.id, "TP2")
                with st.popover("✋ Hold", width="content"):
                    st.markdown("**Choose hold mode**")
                    if st.button("✋ Hold indefinitely", key=f"ord_hold_indef_{pos.id}",
                                 disabled=(pos.exit_mode == "MANUAL"), width="content"):
                        _switch_exit_mode(config, pos.id, "MANUAL")
                    if st.button("⏰ Hold to expiry", key=f"ord_hold_exp_{pos.id}",
                                 disabled=(pos.exit_mode == "HOLD_EXPIRY"), width="content"):
                        _switch_exit_mode(config, pos.id, "HOLD_EXPIRY")
                if st.button("✕ Close", key=f"ord_close_{pos.id}",
                             help="Close at market", width="content"):
                    _manual_close(config, pos.id)
                if st.button("🧠 Assess", key=f"ord_assess_{pos.id}",
                             help="Quick AI assessment", width="content"):
                    _quick_assess(config, pos)
            elif pos.state == "WORKING":
                if st.button("🚫 Cancel", key=f"ord_cancel_{pos.id}",
                             help="Cancel working entry order", width="content"):
                    _manual_close(config, pos.id)
                if st.button("🧠 Assess", key=f"ord_assess_wk_{pos.id}",
                             help="Quick AI assessment", width="content"):
                    _quick_assess(config, pos)

        # Show persisted assessment if one exists
        _show_stored_assessment(config, pos)


def _render_trade_history_row(trade) -> None:
    """Render a single trade history card — works for all terminal states."""
    icon, _ = _STATE_BADGE.get(trade.state, ("❓", "grey"))
    bias_emoji = _BIAS_EMOJI.get(trade.bias, "")
    tp2_str = f"{trade.take_profit_2:.2f}" if trade.take_profit_2 else "—"
    entry_display = (
        f"{trade.entry_fill:.2f}" if trade.entry_fill
        else (f"{trade.entry_min:.2f}–{trade.entry_max:.2f}")
    )

    pnl_md = ""
    if trade.entry_fill and trade.exit_price:
        pts = (trade.exit_price - trade.entry_fill) if trade.bias == "LONG" else (trade.entry_fill - trade.exit_price)
        from drift.gui.state import get_tick_value
        usd = pts * get_tick_value(trade.symbol, config) * trade.quantity
        clr = "#52b788" if pts >= 0 else "#e05252"
        pnl_md = f"<span style='color:{clr};white-space:nowrap'>{pts:+.2f} pts (${usd:+.2f})</span>"

    details = []
    if trade.exit_reason:
        details.append(f"Exit: {trade.exit_reason}")
    if trade.reject_reason:
        details.append(f"Reason: {trade.reject_reason}")
    if trade.parent_order_id:
        details.append(f"IB parent={trade.parent_order_id}  tp={trade.tp_order_id}  sl={trade.sl_order_id}")
    if trade.source != "live":
        details.append(f"Source: {trade.source}")

    mode_label = _MODE_LABEL.get(trade.exit_mode, trade.exit_mode or "—")

    with st.container(border=True):
        c0, c1, c2 = st.columns([3, 5, 2], vertical_alignment="top")
        c0.markdown(
            f"{icon} {bias_emoji} **{trade.bias} {trade.symbol}**  \n"
            f"<small style='color:#aaa'>`{trade.setup_type}` · {trade.confidence}%"
            f" · {mode_label} · **{trade.state}**</small>",
            unsafe_allow_html=True,
        )
        c1.markdown(
            f"<small style='color:#aaa'>Entry</small> **{entry_display}** &ensp;"
            f"<small style='color:#e05252'>SL</small> **{trade.stop_loss:.2f}**<br>"
            f"<small style='color:#52b788'>TP1</small> **{trade.take_profit_1:.2f}** &ensp;"
            f"<small style='color:#52b788'>TP2</small> **{tp2_str}**"
            + (f"&ensp; → exit **{trade.exit_price:.2f}**" if trade.exit_price else ""),
            unsafe_allow_html=True,
        )
        c2.markdown(
            (pnl_md + "<br>" if pnl_md else "") +
            f"<small style='color:#666'>{_age_label(trade.created_at)}</small>",
            unsafe_allow_html=True,
        )
        if details:
            st.caption("  ·  ".join(details))
