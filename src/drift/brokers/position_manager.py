"""Position manager — orchestrates the full order lifecycle.

Responsibilities:
    1. Validate pending orders at approval time (time horizon, price bounds)
    2. Create active position records after successful bracket placement
    3. Detect entry fills via IB polling and transition WORKING → FILLED
    4. Handle exit mode changes (TP1 ↔ TP2 ↔ MANUAL)
    5. Handle manual close (market order)
    6. Detect exit fills (TP/SL) and mark positions closed
    7. Duplicate position guard
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class PositionManager:
    """Stateless orchestrator — each method connects to IB as needed."""

    def __init__(self, config: Any, db_path: str | Path) -> None:
        from drift.storage.pending_order_store import PendingOrderStore
        from drift.storage.position_store import PositionStore

        self._cfg = config
        self._pending = PendingOrderStore(db_path)
        self._positions = PositionStore(db_path)

    # ------------------------------------------------------------------
    # Approval-time validation
    # ------------------------------------------------------------------

    def validate_for_approval(self, order) -> list[str]:
        """Return a list of warnings/errors.  Empty list = safe to proceed."""
        errors: list[str] = []

        # Duplicate position guard
        if self._positions.has_open_position():
            errors.append("An active position is already open. Close it before approving a new trade.")

        # Time horizon check
        if order.generated_at:
            try:
                gen = datetime.fromisoformat(order.generated_at)
                if gen.tzinfo is None:
                    gen = gen.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(tz=timezone.utc) - gen).total_seconds() / 60
                if elapsed > order.max_hold_minutes:
                    errors.append(
                        f"Trade plan expired — generated {elapsed:.0f}m ago, "
                        f"max hold is {order.max_hold_minutes}m."
                    )
            except (ValueError, TypeError):
                pass  # can't parse, skip check

        return errors

    def check_price_validity(self, order, current_price: float) -> list[str]:
        """Check if current price is within the entry zone.  Returns warnings."""
        warnings: list[str] = []
        if current_price < order.entry_min or current_price > order.entry_max:
            warnings.append(
                f"Current price ({current_price:.2f}) is outside entry zone "
                f"[{order.entry_min:.2f} – {order.entry_max:.2f}]. "
                f"The limit order will only fill if price returns to the zone."
            )
        return warnings

    # ------------------------------------------------------------------
    # Approve & place
    # ------------------------------------------------------------------

    def approve_and_place(self, order) -> dict[str, Any]:
        """Full approval flow: validate → place bracket → create position.

        Returns:
            {"status": "ok", "position_id": int, ...} or {"status": "error", "message": str}
        """
        from drift.brokers.ib_client import IBClient

        # Pre-flight
        errors = self.validate_for_approval(order)
        if errors:
            return {"status": "error", "message": " | ".join(errors)}

        # Place bracket via IB
        self._pending.set_state(order.id, "APPROVED")
        client = IBClient(self._cfg.broker)

        result = client.submit_bracket(order)
        if result["status"] != "ok":
            self._pending.set_state(order.id, "FAILED", reject_reason=result["message"])
            return result

        # Update pending order
        self._pending.set_state(
            order.id, "SUBMITTED",
            ib_order_id=result["order_id"],
            ib_perm_id=result["perm_id"],
        )

        # Create active position
        pos_id = self._positions.create(
            pending_order_id=order.id,
            signal_key=order.signal_key,
            symbol=order.symbol,
            bias=order.bias,
            setup_type=order.setup_type,
            entry_limit=order.entry_max if order.bias == "LONG" else order.entry_min,
            stop_loss=order.stop_loss,
            take_profit_1=order.take_profit_1,
            take_profit_2=order.take_profit_2,
            max_hold_minutes=order.max_hold_minutes,
            thesis=order.thesis,
            parent_order_id=result["order_id"],
            tp_order_id=result.get("tp_order_id"),
            sl_order_id=result.get("sl_order_id"),
        )

        log.info(
            "Position %d created (WORKING) from pending order %d",
            pos_id, order.id,
        )
        return {
            "status": "ok",
            "position_id": pos_id,
            "order_id": result["order_id"],
            "perm_id": result["perm_id"],
        }

    # ------------------------------------------------------------------
    # Exit mode changes
    # ------------------------------------------------------------------

    def switch_exit_mode(self, position_id: int, new_mode: str) -> dict[str, Any]:
        """Switch exit mode for a FILLED position.

        new_mode: "TP1" | "TP2" | "MANUAL"
        """
        from drift.brokers.ib_client import IBClient

        pos = self._positions.get_by_id(position_id)
        if not pos or pos.state != "FILLED":
            return {"status": "error", "message": "Position not found or not FILLED."}

        if new_mode == pos.exit_mode:
            return {"status": "ok", "message": "Already in this mode."}

        client = IBClient(self._cfg.broker)

        if new_mode == "TP1":
            if not pos.take_profit_1:
                return {"status": "error", "message": "No TP1 price available."}
            if pos.exit_mode == "MANUAL":
                # Place new TP1 order
                result = client.modify_tp(
                    old_tp_order_id=0,  # no existing TP to cancel
                    new_tp_price=pos.take_profit_1,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                )
            else:
                # TP2 → TP1: modify existing
                result = client.modify_tp(
                    old_tp_order_id=pos.tp_order_id or 0,
                    new_tp_price=pos.take_profit_1,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                )
            if result["status"] != "ok":
                return result
            self._positions.set_exit_mode(
                position_id, "TP1", pos.take_profit_1,
                tp_order_id=result.get("tp_order_id"),
            )

        elif new_mode == "TP2":
            if not pos.take_profit_2:
                return {"status": "error", "message": "No TP2 price available."}
            if pos.exit_mode == "MANUAL":
                result = client.modify_tp(
                    old_tp_order_id=0,
                    new_tp_price=pos.take_profit_2,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                )
            else:
                result = client.modify_tp(
                    old_tp_order_id=pos.tp_order_id or 0,
                    new_tp_price=pos.take_profit_2,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                )
            if result["status"] != "ok":
                return result
            self._positions.set_exit_mode(
                position_id, "TP2", pos.take_profit_2,
                tp_order_id=result.get("tp_order_id"),
            )

        elif new_mode == "MANUAL":
            if pos.tp_order_id:
                result = client.cancel_tp(pos.tp_order_id)
                if result["status"] != "ok":
                    return result
            self._positions.set_exit_mode(position_id, "MANUAL", None)

        else:
            return {"status": "error", "message": f"Unknown mode: {new_mode}"}

        log.info("Position %d exit mode changed: %s → %s", position_id, pos.exit_mode, new_mode)
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Manual close
    # ------------------------------------------------------------------

    def manual_close(self, position_id: int) -> dict[str, Any]:
        """Close position immediately via market order.  Also cancels working SL/TP."""
        from drift.brokers.ib_client import IBClient

        pos = self._positions.get_by_id(position_id)
        if not pos or pos.state not in ("WORKING", "FILLED"):
            return {"status": "error", "message": "Position not found or already closed."}

        client = IBClient(self._cfg.broker)

        if pos.state == "WORKING":
            # Not yet filled — just cancel the bracket
            if pos.parent_order_id:
                result = client.cancel_bracket(pos.parent_order_id)
            else:
                result = {"status": "ok"}
            self._positions.close_position(position_id, "CLOSED_CANCEL", exit_reason="Operator cancelled before fill")
            return result

        # FILLED — close with market order
        result = client.close_position(pos.bias, pos.quantity)
        if result["status"] == "ok":
            self._positions.close_position(
                position_id, "CLOSED_MANUAL",
                exit_price=result.get("fill_price"),
                exit_reason="Operator manual close",
            )
        return result

    # ------------------------------------------------------------------
    # Fill detection (call from polling loop)
    # ------------------------------------------------------------------

    def poll_positions(self) -> list[dict[str, Any]]:
        """Check IB for status updates on all open positions.

        Returns a list of state changes detected.
        """
        from drift.brokers.ib_client import IBClient

        changes: list[dict[str, Any]] = []
        open_positions = self._positions.get_open()
        if not open_positions:
            return changes

        client = IBClient(self._cfg.broker)

        for pos in open_positions:
            if pos.state == "WORKING" and pos.parent_order_id:
                result = client.check_order_status(pos.parent_order_id)
                if result.get("order_status") == "Filled":
                    fill_price = result.get("avg_fill_price") or pos.entry_limit
                    self._positions.mark_filled(pos.id, fill_price)
                    changes.append({
                        "type": "ENTRY_FILLED",
                        "position_id": pos.id,
                        "fill_price": fill_price,
                    })
                    log.info("Position %d ENTRY FILLED at %.2f", pos.id, fill_price)

            elif pos.state == "FILLED":
                # Check SL
                if pos.sl_order_id:
                    sl_result = client.check_order_status(pos.sl_order_id)
                    if sl_result.get("order_status") == "Filled":
                        exit_price = sl_result.get("avg_fill_price") or pos.stop_loss
                        self._positions.close_position(
                            pos.id, "CLOSED_SL", exit_price=exit_price,
                            exit_reason="Stop loss triggered",
                        )
                        changes.append({
                            "type": "SL_HIT",
                            "position_id": pos.id,
                            "exit_price": exit_price,
                        })
                        log.info("Position %d STOP LOSS at %.2f", pos.id, exit_price)
                        continue

                # Check TP
                if pos.tp_order_id:
                    tp_result = client.check_order_status(pos.tp_order_id)
                    if tp_result.get("order_status") == "Filled":
                        exit_price = tp_result.get("avg_fill_price") or pos.active_tp
                        close_state = f"CLOSED_{pos.exit_mode}"
                        self._positions.close_position(
                            pos.id, close_state, exit_price=exit_price,
                            exit_reason=f"{pos.exit_mode} target hit",
                        )
                        changes.append({
                            "type": f"{pos.exit_mode}_HIT",
                            "position_id": pos.id,
                            "exit_price": exit_price,
                        })
                        log.info("Position %d %s HIT at %.2f", pos.id, pos.exit_mode, exit_price)

        return changes

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_open_positions(self):
        return self._positions.get_open()

    def get_filled_positions(self):
        return self._positions.get_filled()

    def has_open_position(self) -> bool:
        return self._positions.has_open_position()

    def get_position(self, position_id: int):
        return self._positions.get_by_id(position_id)

    def get_position_history(self, limit: int = 50):
        return self._positions.get_all(limit)

    def close(self) -> None:
        self._pending.close()
        self._positions.close()
