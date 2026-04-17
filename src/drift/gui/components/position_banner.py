"""Global position banner — shows active positions on every page.

Call ``render_position_banner()`` from ``gui/app.py`` (after page config, before
navigation) so it appears at the top of every Streamlit page regardless of which
page the user is viewing.

Features:
    - Direction, symbol, entry price, current P&L
    - Active exit mode indicator (TP1 / TP2 / MANUAL)
    - Time remaining in hold window
    - Quick-action buttons: Switch TP, Manual Close
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import streamlit as st

from drift.gui.state import get_config, _PROJECT_ROOT

log = logging.getLogger(__name__)

_BIAS_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}
_MODE_LABEL = {"TP1": "🎯 TP1", "TP2": "🎯🎯 TP2", "MANUAL": "✋ Manual Hold"}


def render_position_banner() -> None:
    """Render a persistent position banner if broker is enabled and positions are open."""
    config = get_config()
    if not config.broker.enabled:
        return

    try:
        from drift.storage.position_store import PositionStore

        db_path = _PROJECT_ROOT / config.storage.sqlite_path
        store = PositionStore(str(db_path))
        open_positions = store.get_open()
        store.close()
    except Exception:  # noqa: BLE001
        return

    if not open_positions:
        return

    for pos in open_positions:
        _render_single_position(config, pos)


def _render_single_position(config, pos) -> None:
    """Render one position as a compact banner bar."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")
    mode_label = _MODE_LABEL.get(pos.exit_mode, pos.exit_mode)

    # Compute P&L if we have a fill price
    pnl_display = ""
    pnl_color = "grey"
    if pos.entry_fill is not None:
        try:
            from drift.data.providers.yfinance_provider import YFinanceProvider
            provider = YFinanceProvider()
            current_price = provider.get_latest_quote(pos.symbol)

            if pos.bias == "LONG":
                pnl_pts = current_price - pos.entry_fill
            else:
                pnl_pts = pos.entry_fill - current_price

            pnl_usd = pnl_pts * 0.50 * pos.quantity  # MNQ = $0.50/point
            pnl_color = "green" if pnl_pts >= 0 else "red"
            pnl_display = f"**{pnl_pts:+.2f} pts** (${pnl_usd:+.2f})"
        except Exception:  # noqa: BLE001
            pnl_display = "P&L unavailable"

    # Time remaining
    time_display = ""
    if pos.fill_time and pos.max_hold_minutes:
        try:
            fill_dt = datetime.fromisoformat(pos.fill_time)
            if fill_dt.tzinfo is None:
                fill_dt = fill_dt.replace(tzinfo=timezone.utc)
            elapsed_min = (datetime.now(tz=timezone.utc) - fill_dt).total_seconds() / 60
            remaining = pos.max_hold_minutes - elapsed_min
            if remaining > 0:
                time_display = f"{remaining:.0f}m left"
            else:
                time_display = "⚠️ Hold window expired"
        except (ValueError, TypeError):
            pass

    # Banner container
    with st.container():
        state_label = "⏳ Entry pending" if pos.state == "WORKING" else f"📊 Active"

        # Main info row
        cols = st.columns([3, 2, 2, 2, 2, 1])
        cols[0].markdown(
            f"{bias_emoji} **{pos.bias} {pos.symbol}** — {state_label}"
        )
        if pos.entry_fill is not None:
            cols[1].markdown(f"Entry: **{pos.entry_fill:.2f}**")
        else:
            cols[1].markdown(f"Limit: **{pos.entry_limit:.2f}**")
        if pnl_display:
            cols[2].markdown(f":{pnl_color}[{pnl_display}]")
        cols[3].markdown(mode_label)
        if time_display:
            cols[4].markdown(time_display)

        # Action buttons (only for FILLED positions)
        if pos.state == "FILLED":
            btn_cols = st.columns([1, 1, 1, 1, 4])

            if pos.exit_mode != "TP1" and pos.take_profit_1:
                if btn_cols[0].button("🎯 TP1", key=f"banner_tp1_{pos.id}"):
                    _switch_mode(config, pos.id, "TP1")

            if pos.exit_mode != "TP2" and pos.take_profit_2:
                if btn_cols[1].button("🎯🎯 TP2", key=f"banner_tp2_{pos.id}"):
                    _switch_mode(config, pos.id, "TP2")

            if pos.exit_mode != "MANUAL":
                if btn_cols[2].button("✋ Hold", key=f"banner_hold_{pos.id}"):
                    _switch_mode(config, pos.id, "MANUAL")

            if btn_cols[3].button("🚪 Close Now", key=f"banner_close_{pos.id}", type="primary"):
                _manual_close(config, pos.id)

        st.divider()


def _switch_mode(config, position_id: int, mode: str) -> None:
    """Switch exit mode via PositionManager."""
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
    """Close position via PositionManager."""
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
