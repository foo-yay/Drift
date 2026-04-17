"""Dev Tools — seed and clear test data for manual GUI testing.

This page is only visible when app.mode is NOT 'live'.
It does NOT make any real IB connections.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from drift.gui.state import get_config, _PROJECT_ROOT


@st.cache_resource
def _load_config():
    return get_config()


def _db_path(config) -> str:
    return str(_PROJECT_ROOT / config.storage.sqlite_path)


# ------------------------------------------------------------------
# Seed helpers
# ------------------------------------------------------------------

def _seed_filled_position(config, bias: str = "LONG") -> str:
    from drift.storage.position_store import PositionStore

    prices = {
        "LONG":  dict(entry=19_852.0, sl=19_820.0, tp1=19_905.0, tp2=19_945.0),
        "SHORT": dict(entry=19_848.0, sl=19_880.0, tp1=19_795.0, tp2=19_755.0),
    }[bias]

    store = PositionStore(_db_path(config))
    pos_id = store.create(
        pending_order_id=0,
        signal_key=f"MNQ:{bias}:pullback_continuation:DEVTEST",
        symbol="MNQ",
        bias=bias,
        setup_type="pullback_continuation",
        entry_limit=prices["entry"],
        stop_loss=prices["sl"],
        take_profit_1=prices["tp1"],
        take_profit_2=prices["tp2"],
        max_hold_minutes=30,
        thesis=(
            "Dev-test position seeded manually.  "
            "Price was holding cleanly above 9EMA after a 3-bar pullback on 5m. "
            "Volume on the reclaim bar was 1.4× average."
        ),
    )
    store.mark_filled(
        pos_id,
        fill_price=prices["entry"],
        fill_time=datetime.now(tz=timezone.utc).isoformat(),
    )
    return f"Created {bias} FILLED position id={pos_id}"


def _seed_pending_order(config, bias: str = "LONG") -> str:
    from drift.storage.pending_order_store import PendingOrderStore

    if bias == "LONG":
        row = dict(
            entry_min=19_840.0, entry_max=19_860.0,
            stop_loss=19_815.0, take_profit_1=19_905.0, take_profit_2=19_945.0,
            setup_type="vwap_reclaim",
        )
    else:
        row = dict(
            entry_min=19_840.0, entry_max=19_860.0,
            stop_loss=19_885.0, take_profit_1=19_795.0, take_profit_2=19_755.0,
            setup_type="failed_breakout_reversion",
        )

    store = PendingOrderStore(_db_path(config))
    store.create(
        signal_key=f"MNQ:{bias}:{row['setup_type']}:DEVTEST",
        symbol="MNQ",
        bias=bias,
        setup_type=row["setup_type"],
        entry_min=row["entry_min"],
        entry_max=row["entry_max"],
        stop_loss=row["stop_loss"],
        take_profit_1=row["take_profit_1"],
        take_profit_2=row["take_profit_2"],
        confidence=74,
        thesis=(
            f"Dev-test pending {bias} order.  "
            "R:R looks clean, structure intact, no high-impact events for 25 minutes."
        ),
        max_hold_minutes=25,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
    )
    return f"Created PENDING {bias} order"


def _seed_working_position(config, bias: str = "LONG") -> str:
    """WORKING = entry order placed but not yet filled."""
    from drift.storage.position_store import PositionStore

    prices = {
        "LONG":  dict(entry=19_852.0, sl=19_820.0, tp1=19_905.0, tp2=19_945.0),
        "SHORT": dict(entry=19_848.0, sl=19_880.0, tp1=19_795.0, tp2=19_755.0),
    }[bias]

    store = PositionStore(_db_path(config))
    pos_id = store.create(
        pending_order_id=0,
        signal_key=f"MNQ:{bias}:breakout_continuation:DEVTEST",
        symbol="MNQ",
        bias=bias,
        setup_type="breakout_continuation",
        entry_limit=prices["entry"],
        stop_loss=prices["sl"],
        take_profit_1=prices["tp1"],
        take_profit_2=prices["tp2"],
        max_hold_minutes=30,
        thesis="Dev-test WORKING position (entry limit not yet filled).",
        parent_order_id=99901,
        tp_order_id=99902,
        sl_order_id=99903,
    )
    return f"Created {bias} WORKING position id={pos_id}"


def _clear_test_data(config) -> str:
    import sqlite3
    conn = sqlite3.connect(_db_path(config))
    ap = conn.execute(
        "DELETE FROM active_positions WHERE signal_key LIKE '%DEVTEST%'"
    ).rowcount
    po = conn.execute(
        "DELETE FROM pending_orders WHERE signal_key LIKE '%DEVTEST%'"
    ).rowcount
    conn.commit()
    conn.close()
    return f"Removed {ap} active position(s) and {po} pending order(s)."


# ------------------------------------------------------------------
# Page
# ------------------------------------------------------------------

def page() -> None:
    config = _load_config()

    st.title("🛠️ Dev Tools")
    st.caption(
        "Test data controls — **not visible in live mode**. "
        "No real IB connections are made by any button here."
    )

    if config.app.mode == "live":
        st.error("Dev Tools are disabled in **live** mode.", icon="⛔")
        return

    st.info(
        f"Current mode: **{config.app.mode}** · DB: `{config.storage.sqlite_path}`",
        icon="ℹ️",
    )

    # ------------------------------------------------------------------
    # Section 1 — Active Positions
    # ------------------------------------------------------------------
    st.subheader("Active Positions")
    st.caption("Seeds directly into the positions table with state=FILLED or WORKING. "
               "Check the Orders page and banner after seeding.")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if st.button("🟢 Seed LONG (filled)", use_container_width=True):
            msg = _seed_filled_position(config, "LONG")
            st.success(msg)
            st.rerun()

    with col2:
        if st.button("🔴 Seed SHORT (filled)", use_container_width=True):
            msg = _seed_filled_position(config, "SHORT")
            st.success(msg)
            st.rerun()

    with col3:
        if st.button("⏳ Seed LONG (working)", use_container_width=True):
            msg = _seed_working_position(config, "LONG")
            st.success(msg)
            st.rerun()

    with col4:
        if st.button("⏳ Seed SHORT (working)", use_container_width=True):
            msg = _seed_working_position(config, "SHORT")
            st.success(msg)
            st.rerun()

    # ------------------------------------------------------------------
    # Section 2 — Pending Approvals
    # ------------------------------------------------------------------
    st.subheader("Pending Approvals")
    st.caption("Seeds a pending order into the approvals queue. Go to Orders → Pending Approvals to test Approve/Reject.")

    col5, col6 = st.columns(2)

    with col5:
        if st.button("🟢 Seed LONG pending order", use_container_width=True):
            msg = _seed_pending_order(config, "LONG")
            st.success(msg)
            st.rerun()

    with col6:
        if st.button("🔴 Seed SHORT pending order", use_container_width=True):
            msg = _seed_pending_order(config, "SHORT")
            st.success(msg)
            st.rerun()

    # ------------------------------------------------------------------
    # Section 3 — Current state
    # ------------------------------------------------------------------
    st.subheader("Current Test Data")

    import sqlite3
    conn = sqlite3.connect(_db_path(config))

    ap_rows = conn.execute(
        "SELECT id, bias, state, exit_mode, signal_key FROM active_positions "
        "WHERE signal_key LIKE '%DEVTEST%' ORDER BY created_at DESC"
    ).fetchall()

    po_rows = conn.execute(
        "SELECT id, bias, state, signal_key FROM pending_orders "
        "WHERE signal_key LIKE '%DEVTEST%' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    if ap_rows:
        st.markdown("**Active positions (DEVTEST)**")
        for r in ap_rows:
            st.text(f"  id={r[0]}  bias={r[1]}  state={r[2]}  exit_mode={r[3]}")
    else:
        st.caption("No DEVTEST active positions.")

    if po_rows:
        st.markdown("**Pending orders (DEVTEST)**")
        for r in po_rows:
            st.text(f"  id={r[0]}  bias={r[1]}  state={r[2]}")
    else:
        st.caption("No DEVTEST pending orders.")

    # ------------------------------------------------------------------
    # Section 4 — Cleanup
    # ------------------------------------------------------------------
    st.subheader("Cleanup")

    if st.button("🗑️ Clear all DEVTEST data", type="primary", use_container_width=False):
        msg = _clear_test_data(config)
        st.success(msg)
        st.rerun()
