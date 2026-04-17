"""Tests for order_builder — bracket order construction."""
from __future__ import annotations

import asyncio

# ib_insync requires an event loop at import time (Python 3.14 removed the
# implicit loop creation in the main thread).
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from drift.brokers.order_builder import build_bracket, mnq_contract


def test_mnq_contract():
    c = mnq_contract()
    assert c.symbol == "MNQ"
    assert c.secType == "FUT"
    assert c.exchange == "CME"


def test_build_bracket_long():
    parent, tp, sl = build_bracket(
        bias="LONG",
        entry_limit=21010.0,
        stop_loss=20980.0,
        take_profit=21050.0,
    )
    assert parent.action == "BUY"
    assert parent.lmtPrice == 21010.0
    assert parent.transmit is False

    assert tp.action == "SELL"
    assert tp.lmtPrice == 21050.0
    assert tp.ocaType == 1

    assert sl.action == "SELL"
    assert sl.auxPrice == 20980.0  # StopOrder uses auxPrice
    assert sl.transmit is True


def test_build_bracket_short():
    parent, tp, sl = build_bracket(
        bias="SHORT",
        entry_limit=21000.0,
        stop_loss=21030.0,
        take_profit=20960.0,
    )
    assert parent.action == "SELL"
    assert tp.action == "BUY"
    assert sl.action == "BUY"


def test_oca_group_shared():
    parent, tp, sl = build_bracket(
        bias="LONG", entry_limit=21010.0, stop_loss=20980.0, take_profit=21050.0
    )
    assert tp.ocaGroup == sl.ocaGroup
    assert "drift_" in tp.ocaGroup
