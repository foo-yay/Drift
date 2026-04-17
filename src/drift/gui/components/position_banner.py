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
_MODE_BADGE = {
    "TP1":    "🎯 exit @ TP1",
    "TP2":    "🎯🎯 exit @ TP2",
    "MANUAL": "✋ holding manually",
}


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
    """Render one position as a single compact row."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")

    # P&L
    pnl_md = ""
    if pos.entry_fill is not None:
        try:
            from drift.data.providers.yfinance_provider import YFinanceProvider
            current_price = YFinanceProvider().get_latest_quote(pos.symbol)
            pnl_pts = (current_price - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current_price)
            pnl_usd = pnl_pts * 0.50 * pos.quantity
            color = "green" if pnl_pts >= 0 else "red"
            pnl_md = f":{color}[**{pnl_pts:+.2f} pts** (${pnl_usd:+.2f})]"
        except Exception:  # noqa: BLE001
            pnl_md = "*P&L unavailable*"

    # Time remaining
    time_md = ""
    if pos.fill_time and pos.max_hold_minutes:
        try:
            fill_dt = datetime.fromisoformat(pos.fill_time)
            if fill_dt.tzinfo is None:
                fill_dt = fill_dt.replace(tzinfo=timezone.utc)
            remaining = pos.max_hold_minutes - (datetime.now(tz=timezone.utc) - fill_dt).total_seconds() / 60
            time_md = f"⏱ {remaining:.0f}m" if remaining > 0 else "⚠️ expired"
        except (ValueError, TypeError):
            pass

    entry_md = f"**{pos.entry_fill:.2f}**" if pos.entry_fill is not None else f"limit **{pos.entry_limit:.2f}**"
    state_md = "⏳ pending" if pos.state == "WORKING" else "📊 active"
    mode_md = _MODE_BADGE.get(pos.exit_mode, pos.exit_mode)

    with st.container():
        # Single info row: identity | entry | pnl | mode + time
        c0, c1, c2, c3 = st.columns([3, 2, 2, 3])
        c0.markdown(f"{bias_emoji} **{pos.bias} {pos.symbol}** · {state_md}")
        c1.markdown(f"Entry: {entry_md}")
        if pnl_md:
            c2.markdown(pnl_md)
        c3.markdown(f"{mode_md}{'  ·  ' + time_md if time_md else ''}")

        # Button row — only for filled positions, clearly labelled for what they switch TO
        if pos.state == "FILLED":
            btn_cols = st.columns([1, 1, 1, 1, 4])
            col = 0

            if pos.exit_mode != "TP1" and pos.take_profit_1:
                if btn_cols[col].button(
                    "→ TP1", key=f"banner_tp1_{pos.id}",
                    help=f"Switch exit target to TP1 @ {pos.take_profit_1:.2f}",
                ):
                    _switch_mode(config, pos.id, "TP1")
                col += 1

            if pos.exit_mode != "TP2" and pos.take_profit_2:
                if btn_cols[col].button(
                    "→ TP2", key=f"banner_tp2_{pos.id}",
                    help=f"Switch exit target to TP2 @ {pos.take_profit_2:.2f}",
                ):
                    _switch_mode(config, pos.id, "TP2")
                col += 1

            if pos.exit_mode != "MANUAL":
                if btn_cols[col].button(
                    "✋ Hold", key=f"banner_hold_{pos.id}",
                    help="Cancel auto-exit — hold until you close manually",
                ):
                    _switch_mode(config, pos.id, "MANUAL")
                col += 1

            if btn_cols[col].button(
                "✕ Close", key=f"banner_close_{pos.id}", type="primary",
                help="Submit market order to close this position immediately",
            ):
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
