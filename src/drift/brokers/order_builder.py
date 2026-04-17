"""Interactive Brokers bracket order builder.

Converts a pending order row into an ib_insync bracket order tuple:
    (parent limit order, take-profit limit order, stop-loss stop order)

Parent and children are linked via OCA group so IB automatically cancels
the remaining child when one executes.

Contract: MNQ (Micro E-mini Nasdaq-100 Futures, front-month)
Exchange:  CME  |  Currency: USD  |  SecType: FUT
"""
from __future__ import annotations

import logging
from datetime import date

from ib_insync import Contract, LimitOrder, Order, StopOrder

log = logging.getLogger(__name__)


def _front_month_expiry() -> str:
    """Return the front-month MNQ quarterly expiry as ``YYYYMM``.

    MNQ quarterly months: Mar (03), Jun (06), Sep (09), Dec (12).
    Rolls to the next quarter on the 3rd Friday of the expiry month,
    but we use 1st of the month as a safe roll-over heuristic — IB will
    resolve within the same month correctly.
    """
    today = date.today()
    quarters = [3, 6, 9, 12]
    for q in quarters:
        if today.month < q or (today.month == q and today.day <= 20):
            return f"{today.year}{q:02d}"
    # Past December expiry → next year March
    return f"{today.year + 1}03"


def mnq_contract() -> Contract:
    """Return the MNQ front-month futures contract."""
    return Contract(
        symbol="MNQ",
        secType="FUT",
        exchange="CME",
        currency="USD",
        lastTradeDateOrContractMonth=_front_month_expiry(),
    )


def build_bracket(
    bias: str,
    entry_limit: float,
    stop_loss: float,
    take_profit: float,
    quantity: int = 1,
    account: str = "",
) -> tuple[Order, Order, Order]:
    """Build a bracket order for MNQ.

    Returns:
        (parent, tp_child, sl_child) — pass all three to ``ib.placeOrder()``.

    The children share an OCA group with ``ocaType=1`` (cancel both remaining
    when one fills).  The parent order ID is left at 0; ib_insync assigns a
    real orderId when the order is placed.

    Args:
        bias:         "LONG" or "SHORT"
        entry_limit:  Limit price for the entry order.
                      LONG → entry_max (worst acceptable fill for a buyer)
                      SHORT → entry_min (worst acceptable fill for a seller)
        stop_loss:    Stop price for the protective stop order.
        take_profit:  Limit price for the take-profit order.
        quantity:     Number of contracts (default 1 MNQ).
        account:      IB account number (empty = IB default).
    """
    is_long = bias.upper() == "LONG"
    action = "BUY" if is_long else "SELL"
    exit_action = "SELL" if is_long else "BUY"

    oca_group = f"drift_{int(entry_limit * 100)}"  # stable group name from price

    # Parent: limit entry order
    parent = LimitOrder(
        action=action,
        totalQuantity=quantity,
        lmtPrice=round(entry_limit, 2),
        account=account,
        transmit=False,  # hold until children are attached
    )
    parent.outsideRth = False  # RTH only — MNQ is a CME product, always open

    # TP child: limit exit
    tp_child = LimitOrder(
        action=exit_action,
        totalQuantity=quantity,
        lmtPrice=round(take_profit, 2),
        account=account,
        transmit=False,
        ocaGroup=oca_group,
        ocaType=1,
    )

    # SL child: stop exit (transmit=True triggers the whole bracket)
    sl_child = StopOrder(
        action=exit_action,
        totalQuantity=quantity,
        stopPrice=round(stop_loss, 2),
        account=account,
        transmit=True,
        ocaGroup=oca_group,
        ocaType=1,
    )

    log.info(
        "Built bracket: %s MNQ x%d  entry=%.2f  tp=%.2f  sl=%.2f  oca=%s",
        action, quantity, entry_limit, take_profit, stop_loss, oca_group,
    )
    return parent, tp_child, sl_child
