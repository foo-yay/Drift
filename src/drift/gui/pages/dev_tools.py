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
    from drift.storage.trade_store import TradeStore

    prices = {
        "LONG":  dict(entry=19_852.0, sl=19_820.0, tp1=19_905.0, tp2=19_945.0),
        "SHORT": dict(entry=19_848.0, sl=19_880.0, tp1=19_795.0, tp2=19_755.0),
    }[bias]

    ts = int(datetime.now(tz=timezone.utc).timestamp())
    store = TradeStore(_db_path(config))
    trade_id = store.create(
        signal_key=f"MNQ:{bias}:pullback_continuation:DEV:{ts}",
        symbol="MNQ",
        bias=bias,
        setup_type="pullback_continuation",
        entry_min=prices["entry"],
        entry_max=prices["entry"],
        stop_loss=prices["sl"],
        take_profit_1=prices["tp1"],
        take_profit_2=prices["tp2"],
        thesis=(
            "Dev-test position seeded manually.  "
            "Price was holding cleanly above 9EMA after a 3-bar pullback on 5m. "
            "Volume on the reclaim bar was 1.4× average."
        ),
        source="dev",
        state="WORKING",
        entry_limit=prices["entry"],
        max_hold_minutes=30,
    )
    store.mark_filled(
        trade_id,
        fill_price=prices["entry"],
        fill_time=datetime.now(tz=timezone.utc).isoformat(),
    )
    store.close()
    return f"Created {bias} FILLED trade id={trade_id}"


def _seed_pending_order(config, bias: str = "LONG") -> str:
    from drift.storage.trade_store import TradeStore

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

    ts = int(datetime.now(tz=timezone.utc).timestamp())
    store = TradeStore(_db_path(config))
    store.create(
        signal_key=f"MNQ:{bias}:{row['setup_type']}:DEV:{ts}",
        symbol="MNQ",
        bias=bias,
        setup_type=row["setup_type"],
        entry_min=row["entry_min"],
        entry_max=row["entry_max"],
        stop_loss=row["stop_loss"],
        take_profit_1=row["take_profit_1"],
        take_profit_2=row["take_profit_2"],
        thesis=(
            f"Dev-test pending {bias} order.  "
            "R:R looks clean, structure intact, no high-impact events for 25 minutes."
        ),
        confidence=74,
        max_hold_minutes=25,
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        source="dev",
    )
    store.close()
    return f"Created PENDING {bias} trade"


def _seed_working_position(config, bias: str = "LONG") -> str:
    """WORKING = entry order placed but not yet filled."""
    from drift.storage.trade_store import TradeStore

    prices = {
        "LONG":  dict(entry=19_852.0, sl=19_820.0, tp1=19_905.0, tp2=19_945.0),
        "SHORT": dict(entry=19_848.0, sl=19_880.0, tp1=19_795.0, tp2=19_755.0),
    }[bias]

    ts = int(datetime.now(tz=timezone.utc).timestamp())
    store = TradeStore(_db_path(config))
    trade_id = store.create(
        signal_key=f"MNQ:{bias}:breakout_continuation:DEV:{ts}",
        symbol="MNQ",
        bias=bias,
        setup_type="breakout_continuation",
        entry_min=prices["entry"],
        entry_max=prices["entry"],
        stop_loss=prices["sl"],
        take_profit_1=prices["tp1"],
        take_profit_2=prices["tp2"],
        thesis="Dev-test WORKING trade (entry limit not yet filled).",
        source="dev",
        state="WORKING",
        entry_limit=prices["entry"],
        max_hold_minutes=30,
        parent_order_id=99901,
        tp_order_id=99902,
        sl_order_id=99903,
    )
    store.close()
    return f"Created {bias} WORKING trade id={trade_id}"


def _fire_real_bracket(config, bias: str) -> dict:
    """Fetch live price, build tight bracket, submit to IB Gateway, store trade."""
    from types import SimpleNamespace

    from drift.brokers.ib_client import IBClient
    from drift.data.providers.yfinance_provider import YFinanceProvider
    from drift.storage.trade_store import TradeStore

    # Delayed quote (~15 min) — add buffer so limit fills even with drift
    current = YFinanceProvider().get_latest_quote("MNQ")

    def _tick(p: float) -> float:
        """Round to MNQ 0.25-point tick."""
        return round(round(p * 4) / 4, 2)

    if bias == "LONG":
        entry = _tick(current + 15)
        sl    = _tick(entry - 30)
        tp1   = _tick(entry + 20)
        tp2   = _tick(entry + 40)
    else:
        entry = _tick(current - 15)
        sl    = _tick(entry + 30)
        tp1   = _tick(entry - 20)
        tp2   = _tick(entry - 40)

    fake_order = SimpleNamespace(
        bias=bias,
        entry_max=entry,
        entry_min=entry,
        stop_loss=sl,
        take_profit_1=tp1,
    )

    result = IBClient(config.broker).submit_bracket(fake_order)
    if result["status"] != "ok":
        result["_prices"] = dict(current=current, entry=entry, sl=sl, tp1=tp1, tp2=tp2)
        return result

    # Record in trade store so fill-detection and banner pick it up
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    store = TradeStore(_db_path(config))
    trade_id = store.create(
        signal_key=f"MNQ:{bias}:ib_bracket_test:DEV:{ts}",
        symbol="MNQ",
        bias=bias,
        setup_type="ib_bracket_test",
        entry_min=entry,
        entry_max=entry,
        stop_loss=sl,
        take_profit_1=tp1,
        take_profit_2=tp2,
        thesis="Dev Tools live IB bracket test — placed via Dev Tools page.",
        source="ib_test",
        state="WORKING",
        entry_limit=entry,
        max_hold_minutes=30,
        parent_order_id=result["order_id"],
        tp_order_id=result["tp_order_id"],
        sl_order_id=result["sl_order_id"],
    )
    store.close()

    result["pos_id"] = trade_id
    result["_prices"] = dict(current=current, entry=entry, sl=sl, tp1=tp1, tp2=tp2)
    return result


def _clear_test_data(config) -> str:
    import sqlite3
    conn = sqlite3.connect(_db_path(config))
    n = conn.execute(
        "DELETE FROM trades WHERE signal_key LIKE '%DEV:%' OR signal_key LIKE '%DEVTEST%'"
    ).rowcount
    conn.commit()
    conn.close()
    return f"Removed {n} dev trade(s)."


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

    rows = conn.execute(
        "SELECT id, bias, state, exit_mode, source, signal_key FROM trades "
        "WHERE signal_key LIKE '%DEV:%' OR signal_key LIKE '%DEVTEST%' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    if rows:
        st.markdown("**Dev trades (DEVTEST)**")
        for r in rows:
            st.text(f"  id={r[0]}  bias={r[1]}  state={r[2]}  exit={r[3]}  src={r[4]}")
    else:
        st.caption("No DEVTEST trades.")

    # ------------------------------------------------------------------
    # Section 3b — Real IB bracket orders
    # ------------------------------------------------------------------
    st.subheader("Live IB Test Orders")
    st.caption(
        "Fetches the current MNQ price via yfinance (≈15 min delayed) and places a **real bracket order** "
        "on your paper account through IB Gateway. The position is recorded in the DB so the banner "
        "and fill-detection pick it up. Gateway must be running."
    )

    if not config.broker.enabled:
        st.warning("Broker is disabled in settings — enable it to place real IB orders.", icon="⚠️")
    else:
        ib_col1, ib_col2 = st.columns(2)
        with ib_col1:
            if st.button("🟢 Place real LONG bracket (IB)", use_container_width=True):
                with st.spinner("Connecting to IB Gateway and placing bracket..."):
                    res = _fire_real_bracket(config, "LONG")
                p = res.get("_prices", {})
                if res["status"] == "ok":
                    st.success(
                        f"Bracket placed! parent={res['order_id']} tp={res['tp_order_id']} sl={res['sl_order_id']}  "
                        f"entry={p.get('entry')} · sl={p.get('sl')} · tp1={p.get('tp1')} "
                        f"(quoted @ {p.get('current'):.2f})"
                    )
                    st.rerun()
                else:
                    st.error(f"IB error: {res.get('message')}")
                    if p:
                        st.caption(f"Prices attempted — entry={p.get('entry')} sl={p.get('sl')} tp1={p.get('tp1')} (quoted @ {p.get('current'):.2f})")

        with ib_col2:
            if st.button("🔴 Place real SHORT bracket (IB)", use_container_width=True):
                with st.spinner("Connecting to IB Gateway and placing bracket..."):
                    res = _fire_real_bracket(config, "SHORT")
                p = res.get("_prices", {})
                if res["status"] == "ok":
                    st.success(
                        f"Bracket placed! parent={res['order_id']} tp={res['tp_order_id']} sl={res['sl_order_id']}  "
                        f"entry={p.get('entry')} · sl={p.get('sl')} · tp1={p.get('tp1')} "
                        f"(quoted @ {p.get('current'):.2f})"
                    )
                    st.rerun()
                else:
                    st.error(f"IB error: {res.get('message')}")
                    if p:
                        st.caption(f"Prices attempted — entry={p.get('entry')} sl={p.get('sl')} tp1={p.get('tp1')} (quoted @ {p.get('current'):.2f})")

    # ------------------------------------------------------------------
    # Section 4 — Cleanup
    # ------------------------------------------------------------------
    st.subheader("Cleanup")

    if st.button("🗑️ Clear all DEVTEST data", type="primary", use_container_width=False):
        msg = _clear_test_data(config)
        st.success(msg)
        st.rerun()
