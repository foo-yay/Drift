"""Global position banner — shows active positions on every page.

Call ``render_position_banner()`` from ``gui/app.py`` (after page config, before
navigation) so it appears at the top of every Streamlit page regardless of which
page the user is viewing.

Renders as one compact row per open position with action buttons inline.
Auto-refreshes P&L every 30 s via st.fragment without a full page reload.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import streamlit as st

from drift.gui.state import get_config, _PROJECT_ROOT

log = logging.getLogger(__name__)

_BIAS_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}

# Align text rows with button rows inside st.columns
_VALIGN_CSS = """
<style>
[data-testid="stHorizontalBlock"] { align-items: center !important; }
</style>
"""


def _time_display(pos) -> str:
    """Return a time string for the hold window.

    MANUAL mode: show how far past (or short of) the window we are.
    TP1/TP2 mode: show time remaining (auto-exit is still armed).
    Never implies auto-close on expiry — that doesn't happen.
    """
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
        # Past window
        over = abs(remaining)
        if pos.exit_mode == "MANUAL":
            return f"✋ +{over:.0f}m past window"
        return f"⚠️ +{over:.0f}m past window"
    except (ValueError, TypeError):
        return ""


@st.fragment(run_every=30)
def render_position_banner() -> None:
    """Render a compact per-position row banner. Reruns every 30 s for P&L."""
    config = get_config()
    if not config.broker.enabled:
        return

    try:
        from drift.storage.position_store import PositionStore

        store = PositionStore(str(_PROJECT_ROOT / config.storage.sqlite_path))
        open_positions = store.get_open()
        store.close()
    except Exception:  # noqa: BLE001
        return

    if not open_positions:
        return

    st.markdown(_VALIGN_CSS, unsafe_allow_html=True)
    for pos in open_positions:
        _render_row(config, pos)


def _render_row(config, pos) -> None:
    """One compact row: identity | price ladder | P&L + time | action buttons."""
    bias_emoji = _BIAS_EMOJI.get(pos.bias, "")
    state_str = "⏳" if pos.state == "WORKING" else "📊"
    entry_str = f"{pos.entry_fill:.2f}" if pos.entry_fill else f"lim {pos.entry_limit:.2f}"

    # Exit mode badge — makes it clear what the current auto-exit target is
    mode_badge = {"TP1": "🎯 TP1", "TP2": "🎯🎯 TP2", "MANUAL": "✋ manual"}.get(pos.exit_mode, pos.exit_mode)

    # Price ladder: Entry · SL · TP1 · TP2
    tp2_str = f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—"
    ladder_md = (
        f"<small style='color:#aaa'>Entry</small> **{entry_str}** &nbsp;"
        f"<small style='color:#e05252'>SL</small> **{pos.stop_loss:.2f}** &nbsp;"
        f"<small style='color:#52b788'>TP1</small> **{pos.take_profit_1:.2f}** &nbsp;"
        f"<small style='color:#52b788'>TP2</small> **{tp2_str}**"
    )

    # P&L
    pnl_md = ""
    if pos.entry_fill:
        try:
            from drift.data.providers.yfinance_provider import YFinanceProvider
            current_price = YFinanceProvider().get_latest_quote(pos.symbol)
            pts = (current_price - pos.entry_fill) if pos.bias == "LONG" else (pos.entry_fill - current_price)
            usd = pts * 0.50 * pos.quantity
            color = "green" if pts >= 0 else "red"
            pnl_md = f":{color}[{pts:+.2f} pts (${usd:+.2f})]"
        except Exception:  # noqa: BLE001
            pnl_md = "P&L —"

    time_md = _time_display(pos)

    # Button count drives column sizing
    n_btns = sum([
        pos.state == "FILLED" and pos.exit_mode != "TP1" and bool(pos.take_profit_1),
        pos.state == "FILLED" and pos.exit_mode != "TP2" and bool(pos.take_profit_2),
        pos.state == "FILLED" and pos.exit_mode != "MANUAL",
        True,  # close/cancel always present
    ])

    cols = st.columns([2, 3.5, 2, 1.5] + [0.8] * n_btns)

    # Col 0: identity
    cols[0].markdown(f"{state_str} {bias_emoji} **{pos.bias} {pos.symbol}** · {mode_badge}")

    # Col 1: price ladder
    cols[1].markdown(ladder_md, unsafe_allow_html=True)

    # Col 2: P&L
    if pnl_md:
        cols[2].markdown(pnl_md)

    # Col 3: time
    if time_md:
        cols[3].markdown(f"<small>{time_md}</small>", unsafe_allow_html=True)

    # Buttons
    btn_col = 4
    if pos.state == "FILLED":
        if pos.exit_mode != "TP1" and pos.take_profit_1:
            if cols[btn_col].button("→TP1", key=f"bn_tp1_{pos.id}",
                                    help=f"Switch exit to TP1 @ {pos.take_profit_1:.2f}"):
                _switch_mode(config, pos.id, "TP1")
            btn_col += 1
        if pos.exit_mode != "TP2" and pos.take_profit_2:
            if cols[btn_col].button("→TP2", key=f"bn_tp2_{pos.id}",
                                    help=f"Switch exit to TP2 @ {pos.take_profit_2:.2f}"):
                _switch_mode(config, pos.id, "TP2")
            btn_col += 1
        if pos.exit_mode != "MANUAL":
            if cols[btn_col].button("✋", key=f"bn_hold_{pos.id}",
                                    help="Hold manually — disarms auto-exit. Position stays open past time window until you close it or SL/TP triggers."):
                _switch_mode(config, pos.id, "MANUAL")
            btn_col += 1
        if cols[btn_col].button("✕", key=f"bn_close_{pos.id}", type="primary",
                                help="Submit market order to close now"):
            _manual_close(config, pos.id)
    else:
        if cols[btn_col].button("✕", key=f"bn_cancel_{pos.id}", type="primary",
                                help="Cancel working entry order"):
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
