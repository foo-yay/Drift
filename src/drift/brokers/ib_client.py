"""Interactive Brokers client — connect-on-demand order lifecycle management.

Design decisions
----------------
- **Connect-on-demand**: we do NOT keep a persistent connection.  Each call
  connects, operates, then disconnects.  This keeps the integration stateless
  and compatible with Streamlit's threading model.
- **Synchronous**: ib_insync has a built-in event loop; we run it in blocking
  mode (``util.startLoop`` is NOT used).
- **Paper trading only** until the account field in config is explicitly set to
  a live-account number.

Capabilities:
    submit_bracket   — place entry + SL + TP bracket
    verify_bracket   — confirm all 3 orders exist after placement
    modify_tp        — cancel current TP, place new TP (for TP1→TP2 switch)
    cancel_tp        — cancel TP order only (for MANUAL hold mode)
    cancel_bracket   — cancel entire bracket (entry not yet filled)
    close_position   — submit market order to close an open position
    check_order_status — poll order status for fill detection
    check_connectivity — quick connection test (pre-flight)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)


def _ensure_event_loop() -> None:
    """Guarantee an asyncio event loop exists in the current thread.

    ib_insync's dependency (eventkit) calls asyncio.get_event_loop() at
    *import* time.  In Python 3.10+ this raises RuntimeError when called from
    a non-main thread (such as Streamlit's ScriptRunner thread) that has no
    loop yet.  Creating one here before the first import sidesteps the crash.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class IBClient:
    """Thin wrapper around ib_insync for connect-on-demand order management."""

    def __init__(self, config: Any) -> None:  # config: BrokerSection
        self._cfg = config

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self):
        """Connect and return (ib, contract) or raise."""
        _ensure_event_loop()
        from ib_insync import IB

        from drift.brokers.gateway_launcher import ensure_gateway_running
        from drift.brokers.order_builder import mnq_contract

        ensure_gateway_running(self._cfg)

        ib = IB()
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
        contract = mnq_contract()
        candidates = ib.qualifyContracts(contract)
        if not candidates:
            ib.disconnect()
            raise RuntimeError("Could not qualify MNQ contract with IB.")
        # Pick front-month: sort by lastTradeDateOrContractMonth ascending
        from datetime import date
        def _expiry(c) -> str:
            return c.lastTradeDateOrContractMonth or "99999999"
        candidates_sorted = sorted(candidates, key=_expiry)
        front_month = candidates_sorted[0]
        log.info("Resolved MNQ contract: %s (expiry %s)", front_month.localSymbol,
                 front_month.lastTradeDateOrContractMonth)
        return ib, front_month

    # ------------------------------------------------------------------
    # Pre-flight connectivity check
    # ------------------------------------------------------------------

    def check_connectivity(self) -> dict[str, Any]:
        """Quick connect/disconnect test.  Returns {"status": "ok"} or error."""
        _ensure_event_loop()
        try:
            from ib_insync import IB
        except ImportError:
            return {"status": "error", "message": "ib_insync not installed"}

        # Trigger auto-start if Gateway isn't running yet
        try:
            from drift.brokers.gateway_launcher import ensure_gateway_running
            ensure_gateway_running(self._cfg)
        except RuntimeError as exc:
            return {"status": "error", "message": str(exc)}

        ib = IB()
        try:
            ib.connect(
                self._cfg.host, self._cfg.port,
                clientId=self._cfg.client_id, timeout=10, readonly=True,
            )
            connected = ib.isConnected()
            ib.disconnect()
            if connected:
                return {"status": "ok"}
            return {"status": "error", "message": "Connected but isConnected() returned False"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def submit_bracket(self, order: Any) -> dict[str, Any]:
        """Connect to IB Gateway, place a bracket order, verify, and disconnect.

        Args:
            order: A ``PendingOrderRow`` from ``PendingOrderStore``.

        Returns:
            dict with keys:
                status    — "ok" | "error"
                order_id  — parent orderId (int) on success
                perm_id   — parent permId (int) on success
                tp_order_id — TP child orderId
                sl_order_id — SL child orderId
                message   — error description on failure
        """
        _ensure_event_loop()
        try:
            from ib_insync import IB
        except ImportError:
            return {"status": "error", "message": "ib_insync is not installed."}

        from drift.brokers.order_builder import build_bracket

        try:
            ib, contract = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection failed: {exc}"}

        try:
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
            tp_trade = ib.placeOrder(contract, tp_child)
            sl_trade = ib.placeOrder(contract, sl_child)

            # Wait for acknowledgement
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

            # Verify all 3 orders exist
            open_orders = ib.openOrders()
            parent_id = parent_trade.order.orderId
            bracket_ids = {parent_id, tp_trade.order.orderId, sl_trade.order.orderId}
            found_ids = {o.orderId for o in open_orders if o.orderId in bracket_ids}

            # If parent already filled, children are "working" — that's OK
            if status == "Filled":
                log.info("Entry already filled — bracket children should be working")
            elif len(found_ids) < 3:
                missing = bracket_ids - found_ids
                log.error("Bracket incomplete! Missing orderIds: %s — cancelling", missing)
                for oid in found_ids:
                    try:
                        ib.cancelOrder(
                            next(o for o in open_orders if o.orderId == oid)
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return {
                    "status": "error",
                    "message": f"Bracket incomplete — missing orderIds {missing}. All cancelled.",
                }

            log.info(
                "Bracket placed & verified: parent=%d tp=%d sl=%d status=%s",
                parent_id, tp_trade.order.orderId, sl_trade.order.orderId, status,
            )
            return {
                "status": "ok",
                "order_id": parent_id,
                "perm_id": parent_trade.order.permId,
                "tp_order_id": tp_trade.order.orderId,
                "sl_order_id": sl_trade.order.orderId,
            }

        except Exception as exc:  # noqa: BLE001
            log.exception("IB bracket placement failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # TP modification (TP1 ↔ TP2 ↔ MANUAL)
    # ------------------------------------------------------------------

    def modify_tp(
        self,
        old_tp_order_id: int,
        new_tp_price: float,
        bias: str,
        parent_order_id: int,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """Cancel the current TP order and place a new one at new_tp_price.

        Returns {"status": "ok", "tp_order_id": <new>} or error.
        """
        try:
            ib, contract = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection failed: {exc}"}

        try:
            from ib_insync import LimitOrder

            # Cancel old TP
            open_orders = ib.openOrders()
            old_tp = next((o for o in open_orders if o.orderId == old_tp_order_id), None)
            if old_tp:
                ib.cancelOrder(old_tp)
                ib.sleep(1)
                log.info("Cancelled old TP order %d", old_tp_order_id)
            else:
                log.warning("Old TP order %d not found in open orders — may already be filled", old_tp_order_id)

            # Place new TP
            exit_action = "SELL" if bias.upper() == "LONG" else "BUY"
            new_tp = LimitOrder(
                action=exit_action,
                totalQuantity=quantity,
                lmtPrice=round(new_tp_price, 2),
                account=self._cfg.account,
                parentId=parent_order_id,
                transmit=True,
            )
            tp_trade = ib.placeOrder(contract, new_tp)
            ib.sleep(1)

            log.info("New TP order placed: id=%d price=%.2f", tp_trade.order.orderId, new_tp_price)
            return {"status": "ok", "tp_order_id": tp_trade.order.orderId}

        except Exception as exc:  # noqa: BLE001
            log.exception("TP modification failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def cancel_tp(self, tp_order_id: int) -> dict[str, Any]:
        """Cancel TP order only (for MANUAL hold mode).  SL stays active."""
        try:
            ib, _ = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection failed: {exc}"}

        try:
            open_orders = ib.openOrders()
            tp_order = next((o for o in open_orders if o.orderId == tp_order_id), None)
            if tp_order:
                ib.cancelOrder(tp_order)
                ib.sleep(1)
                log.info("Cancelled TP order %d for MANUAL mode", tp_order_id)
                return {"status": "ok"}
            log.warning("TP order %d not found — may already be cancelled/filled", tp_order_id)
            return {"status": "ok"}  # idempotent
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Cancel & close
    # ------------------------------------------------------------------

    def cancel_bracket(self, parent_order_id: int) -> dict[str, Any]:
        """Cancel the entire bracket (entry + children).  Use before fill."""
        try:
            ib, _ = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection failed: {exc}"}

        try:
            open_orders = ib.openOrders()
            bracket_orders = [
                o for o in open_orders
                if o.orderId == parent_order_id or getattr(o, "parentId", None) == parent_order_id
            ]
            for o in bracket_orders:
                ib.cancelOrder(o)
            ib.sleep(1)
            log.info("Cancelled bracket (parent=%d, %d orders)", parent_order_id, len(bracket_orders))
            return {"status": "ok", "cancelled": len(bracket_orders)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def close_position(self, bias: str, quantity: int = 1) -> dict[str, Any]:
        """Submit a market order to close an open position immediately."""
        _ensure_event_loop()
        try:
            from ib_insync import MarketOrder
            ib, contract = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection/import failed: {exc}"}

        try:
            exit_action = "SELL" if bias.upper() == "LONG" else "BUY"

            # Cancel any working orders on the exit side first to avoid
            # hitting IB's 15-order-per-side limit.
            open_orders = ib.openOrders()
            for o in open_orders:
                if getattr(o, "action", None) == exit_action:
                    try:
                        ib.cancelOrder(o)
                    except Exception:  # noqa: BLE001
                        pass
            if open_orders:
                ib.sleep(1)

            close_order = MarketOrder(
                action=exit_action,
                totalQuantity=quantity,
                account=self._cfg.account,
            )
            trade = ib.placeOrder(contract, close_order)

            # Wait for fill
            deadline = time.monotonic() + self._cfg.order_timeout_seconds
            while time.monotonic() < deadline:
                ib.sleep(0.5)
                if trade.orderStatus.status == "Filled":
                    break

            if trade.orderStatus.status == "Filled":
                fill_price = trade.orderStatus.avgFillPrice
                log.info("Position closed at market: %.2f", fill_price)
                return {"status": "ok", "fill_price": fill_price}
            return {
                "status": "error",
                "message": f"Market close order status: {trade.orderStatus.status}",
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("Close position failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Cancel all orders (cleanup)
    # ------------------------------------------------------------------

    def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel ALL open orders for MNQ.  Use to clean up orphaned orders."""
        try:
            ib, _ = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection failed: {exc}"}

        try:
            open_orders = ib.openOrders()
            cancelled = 0
            for o in open_orders:
                try:
                    ib.cancelOrder(o)
                    cancelled += 1
                except Exception:  # noqa: BLE001
                    pass
            ib.sleep(1)
            log.info("Cancelled %d open orders", cancelled)
            return {"status": "ok", "cancelled": cancelled}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # SL modification
    # ------------------------------------------------------------------

    def modify_sl(
        self,
        old_sl_order_id: int,
        new_sl_price: float,
        bias: str,
        parent_order_id: int,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """Cancel the current SL order and place a new one at new_sl_price.

        Returns {"status": "ok", "sl_order_id": <new>} or error.
        """
        try:
            ib, contract = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection failed: {exc}"}

        try:
            from ib_insync import StopOrder

            # Cancel old SL
            open_orders = ib.openOrders()
            old_sl = next((o for o in open_orders if o.orderId == old_sl_order_id), None)
            if old_sl:
                ib.cancelOrder(old_sl)
                ib.sleep(1)
                log.info("Cancelled old SL order %d", old_sl_order_id)
            else:
                log.warning("Old SL order %d not found — may already be filled/cancelled", old_sl_order_id)

            # Place new SL
            exit_action = "SELL" if bias.upper() == "LONG" else "BUY"
            new_sl = StopOrder(
                action=exit_action,
                totalQuantity=quantity,
                auxPrice=round(new_sl_price, 2),
                account=self._cfg.account,
                parentId=parent_order_id,
                transmit=True,
            )
            sl_trade = ib.placeOrder(contract, new_sl)
            ib.sleep(1)

            log.info("New SL order placed: id=%d price=%.2f", sl_trade.order.orderId, new_sl_price)
            return {"status": "ok", "sl_order_id": sl_trade.order.orderId}

        except Exception as exc:  # noqa: BLE001
            log.exception("SL modification failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Entry limit modification (WORKING orders only)
    # ------------------------------------------------------------------

    def modify_entry(
        self,
        old_parent_order_id: int,
        new_entry_price: float,
        bias: str,
        stop_loss: float,
        take_profit: float,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """Cancel the current bracket and place a new one at a different entry price.

        This is a full bracket replace — all three orders (entry + TP + SL) are
        cancelled and re-placed.  Only valid for WORKING (unfilled) orders.

        Returns {"status": "ok", "order_id": ..., "tp_order_id": ..., "sl_order_id": ..., "perm_id": ...}
        or error.
        """
        try:
            ib, contract = self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": f"Connection failed: {exc}"}

        try:
            from drift.brokers.order_builder import build_bracket

            # Cancel old bracket
            open_orders = ib.openOrders()
            bracket_orders = [
                o for o in open_orders
                if o.orderId == old_parent_order_id
                or getattr(o, "parentId", None) == old_parent_order_id
            ]
            for o in bracket_orders:
                ib.cancelOrder(o)
            ib.sleep(1)
            log.info("Cancelled old bracket (parent=%d, %d orders)", old_parent_order_id, len(bracket_orders))

            # Place new bracket
            parent, tp_child, sl_child = build_bracket(
                bias=bias,
                entry_limit=new_entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                quantity=quantity,
                account=self._cfg.account,
            )
            parent_trade = ib.placeOrder(contract, parent)
            tp_child.parentId = parent_trade.order.orderId
            sl_child.parentId = parent_trade.order.orderId
            tp_trade = ib.placeOrder(contract, tp_child)
            sl_trade = ib.placeOrder(contract, sl_child)
            ib.sleep(1)

            log.info(
                "New bracket placed: parent=%d tp=%d sl=%d entry=%.2f",
                parent_trade.order.orderId, tp_trade.order.orderId,
                sl_trade.order.orderId, new_entry_price,
            )
            return {
                "status": "ok",
                "order_id": parent_trade.order.orderId,
                "perm_id": parent_trade.order.permId,
                "tp_order_id": tp_trade.order.orderId,
                "sl_order_id": sl_trade.order.orderId,
            }

        except Exception as exc:  # noqa: BLE001
            log.exception("Entry modification failed: %s", exc)
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # Status polling
    # ------------------------------------------------------------------

    def check_order_status(self, order_id: int) -> dict[str, Any]:
        """Check the current status of an order.

        Uses explicit ``reqAllOpenOrders()`` and ``reqCompletedOrders()``
        instead of relying on the session-local caches (``openOrders()`` /
        ``trades()``), which are empty on a fresh readonly connection because
        ib_insync skips auto-subscribe when ``readonly=True``.

        Returns:
            {"status": "ok", "order_status": "Filled"|"Submitted"|..., "avg_fill_price": float|None}
        """
        _ensure_event_loop()
        try:
            from ib_insync import IB
            ib = IB()
            ib.connect(
                self._cfg.host, self._cfg.port,
                clientId=self._cfg.client_id, timeout=10, readonly=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}

        try:
            # Explicitly request all open orders across all client IDs.
            # readonly=True skips reqAutoOpenOrders, so the local caches
            # (openOrders / trades) are empty on a fresh connection.
            open_trades = ib.reqAllOpenOrders()
            for t in open_trades:
                if t.order.orderId == order_id:
                    status = t.orderStatus.status
                    if status == "Filled":
                        return {
                            "status": "ok",
                            "order_status": "Filled",
                            "avg_fill_price": t.orderStatus.avgFillPrice or None,
                        }
                    return {"status": "ok", "order_status": status, "avg_fill_price": None}

            # Not in open orders — check completed (filled/cancelled)
            completed = ib.reqCompletedOrders(apiOnly=True)
            for t in completed:
                if t.order.orderId == order_id:
                    return {
                        "status": "ok",
                        "order_status": t.orderStatus.status,
                        "avg_fill_price": t.orderStatus.avgFillPrice or None,
                    }

            return {"status": "ok", "order_status": "Unknown", "avg_fill_price": None}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
