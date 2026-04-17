"""Interactive Brokers client — connect-on-demand bracket order submission.

Design decisions
----------------
- **Connect-on-demand**: we do NOT keep a persistent connection.  Each approval
  call connects, submits, waits for acknowledgement, then disconnects.  This
  avoids needing the GUI to manage a live socket and keeps the integration
  stateless and restartable.
- **Synchronous**: ib_insync has a built-in event loop; we run it in blocking
  mode (``util.startLoop`` is NOT used so we stay compatible with Streamlit's
  threading model).
- **Paper trading only** until the account field in config is explicitly set to
  a live-account number.

Usage::

    from drift.brokers.ib_client import IBClient
    from drift.config.models import BrokerSection

    client = IBClient(broker_config)
    result = client.submit_bracket(pending_order_row)
    # result: {"status": "ok", "order_id": 1234, "perm_id": 5678}
    # or:     {"status": "error", "message": "..."}
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class IBClient:
    """Thin wrapper around ib_insync for connect-on-demand order placement."""

    def __init__(self, config: Any) -> None:  # config: BrokerSection
        self._cfg = config

    def submit_bracket(self, order: Any) -> dict[str, Any]:
        """Connect to IB Gateway, place a bracket order, and disconnect.

        Args:
            order: A ``PendingOrderRow`` from ``PendingOrderStore``.

        Returns:
            dict with keys:
                status    — "ok" | "error"
                order_id  — IB orderId (int) on success
                perm_id   — IB permId (int) on success
                message   — error description on failure
        """
        try:
            from ib_insync import IB
        except ImportError:
            return {
                "status": "error",
                "message": "ib_insync is not installed. Run: pip install ib_insync",
            }

        from drift.brokers.order_builder import build_bracket, mnq_contract

        ib = IB()
        try:
            log.info(
                "Connecting to IB Gateway at %s:%d (client_id=%d)",
                self._cfg.host, self._cfg.port, self._cfg.client_id,
            )
            ib.connect(
                self._cfg.host,
                self._cfg.port,
                clientId=self._cfg.client_id,
                timeout=self._cfg.order_timeout_seconds,
                readonly=False,
            )

            # Qualify the contract (resolves front-month conId)
            contract = mnq_contract()
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                return {"status": "error", "message": "Could not qualify MNQ contract with IB."}
            contract = qualified[0]
            log.info("Contract qualified: %s conId=%s", contract.localSymbol, contract.conId)

            # Determine entry limit price: worst-case fill side
            entry_limit = order.entry_max if order.bias == "LONG" else order.entry_min

            parent, tp_child, sl_child = build_bracket(
                bias=order.bias,
                entry_limit=entry_limit,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit_1,
                quantity=1,
                account=self._cfg.account,
            )

            # Place parent first, then attach children
            parent_trade = ib.placeOrder(contract, parent)
            tp_child.parentId = parent_trade.order.orderId
            sl_child.parentId = parent_trade.order.orderId
            ib.placeOrder(contract, tp_child)
            ib.placeOrder(contract, sl_child)

            # Give IB a moment to acknowledge
            deadline = time.monotonic() + self._cfg.order_timeout_seconds
            while time.monotonic() < deadline:
                ib.sleep(0.5)
                status = parent_trade.orderStatus.status
                if status in ("Submitted", "PreSubmitted", "Filled"):
                    break
                if status in ("Cancelled", "Inactive"):
                    return {
                        "status": "error",
                        "message": f"Order was {status} by IB immediately after placement.",
                    }

            order_id = parent_trade.order.orderId
            perm_id = parent_trade.order.permId
            log.info(
                "Bracket placed: orderId=%d permId=%d status=%s",
                order_id, perm_id, parent_trade.orderStatus.status,
            )
            return {"status": "ok", "order_id": order_id, "perm_id": perm_id}

        except Exception as exc:  # noqa: BLE001
            log.exception("IB order placement failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
