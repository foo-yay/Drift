"""Trade manager — orchestrates the full trade lifecycle.

Responsibilities:
    1. Validate pending trades at approval time (time horizon, price bounds)
    2. Place bracket orders and transition PENDING → WORKING
    3. Detect entry fills via IB polling and transition WORKING → FILLED
    4. Handle exit mode changes (TP1 ↔ TP2 ↔ MANUAL ↔ HOLD_EXPIRY)
    5. Handle manual close (market order)
    6. Detect exit fills (TP/SL) and mark trades closed
    7. Duplicate trade guard
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
        from drift.storage.trade_store import TradeStore

        self._cfg = config
        self._trades = TradeStore(db_path)

    # ------------------------------------------------------------------
    # Approval-time validation
    # ------------------------------------------------------------------

    def validate_for_approval(self, trade) -> list[str]:
        """Return a list of warnings/errors.  Empty list = safe to proceed."""
        errors: list[str] = []

        # Duplicate trade guard
        if self._trades.has_active_trade():
            errors.append("An active position is already open. Close it before approving a new trade.")

        # Time horizon check
        if trade.generated_at:
            try:
                gen = datetime.fromisoformat(trade.generated_at)
                if gen.tzinfo is None:
                    gen = gen.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(tz=timezone.utc) - gen).total_seconds() / 60
                if elapsed > trade.max_hold_minutes:
                    errors.append(
                        f"Trade plan expired — generated {elapsed:.0f}m ago, "
                        f"max hold is {trade.max_hold_minutes}m."
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

    def approve_and_place(self, trade) -> dict[str, Any]:
        """Full approval flow: validate → place bracket → update trade row.

        Returns:
            {"status": "ok", "trade_id": int, ...} or {"status": "error", "message": str}
        """
        from drift.brokers.ib_client import IBClient

        # Pre-flight
        errors = self.validate_for_approval(trade)
        if errors:
            return {"status": "error", "message": " | ".join(errors)}

        # Place bracket via IB
        self._trades.set_state(trade.id, "APPROVED")
        client = IBClient(self._cfg.broker)

        result = client.submit_bracket(trade)
        if result["status"] != "ok":
            self._trades.set_state(trade.id, "FAILED", reject_reason=result["message"])
            return result

        # Update trade with IB IDs and transition to WORKING
        entry_limit = trade.entry_max if trade.bias == "LONG" else trade.entry_min
        self._trades.set_broker_ids(
            trade.id,
            entry_limit=entry_limit,
            parent_order_id=result["order_id"],
            tp_order_id=result.get("tp_order_id"),
            sl_order_id=result.get("sl_order_id"),
            ib_perm_id=result.get("perm_id"),
        )
        self._trades.set_state(trade.id, "WORKING")

        log.info("Trade %d → WORKING (bracket placed)", trade.id)
        return {
            "status": "ok",
            "trade_id": trade.id,
            # Keep backward-compatible keys for callers
            "position_id": trade.id,
            "order_id": result["order_id"],
            "perm_id": result["perm_id"],
        }

    # ------------------------------------------------------------------
    # Exit mode changes
    # ------------------------------------------------------------------

    def switch_exit_mode(self, position_id: int, new_mode: str) -> dict[str, Any]:
        """Switch exit mode for a FILLED trade.

        new_mode: "TP1" | "TP2" | "MANUAL" | "HOLD_EXPIRY"

        MANUAL      — cancel TP on IB; trade stays open indefinitely until
                      operator closes it or SL fires.
        HOLD_EXPIRY — cancel TP on IB (same IB action as MANUAL); a background
                      daemon auto-closes the trade at max_hold_minutes.
        """
        from drift.brokers.ib_client import IBClient

        _NO_ACTIVE_TP = {"MANUAL", "HOLD_EXPIRY"}

        pos = self._trades.get_by_id(position_id)
        if not pos or pos.state != "FILLED":
            return {"status": "error", "message": "Position not found or not FILLED."}

        if new_mode == pos.exit_mode:
            return {"status": "ok", "message": "Already in this mode."}

        client = IBClient(self._cfg.broker)

        if new_mode == "TP1":
            if not pos.take_profit_1:
                return {"status": "error", "message": "No TP1 price available."}
            if pos.exit_mode in _NO_ACTIVE_TP:
                result = client.modify_tp(
                    old_tp_order_id=0,
                    new_tp_price=pos.take_profit_1,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                )
            else:
                result = client.modify_tp(
                    old_tp_order_id=pos.tp_order_id or 0,
                    new_tp_price=pos.take_profit_1,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                )
            if result["status"] != "ok":
                return result
            self._trades.set_exit_mode(
                position_id, "TP1", pos.take_profit_1,
                tp_order_id=result.get("tp_order_id"),
            )

        elif new_mode == "TP2":
            if not pos.take_profit_2:
                return {"status": "error", "message": "No TP2 price available."}
            if pos.exit_mode in _NO_ACTIVE_TP:
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
            self._trades.set_exit_mode(
                position_id, "TP2", pos.take_profit_2,
                tp_order_id=result.get("tp_order_id"),
            )

        elif new_mode in ("MANUAL", "HOLD_EXPIRY"):
            if pos.tp_order_id:
                result = client.cancel_tp(pos.tp_order_id)
                if result["status"] != "ok":
                    return result
            self._trades.set_exit_mode(position_id, new_mode, None)

        else:
            return {"status": "error", "message": f"Unknown mode: {new_mode}"}

        log.info("Trade %d exit mode changed: %s → %s", position_id, pos.exit_mode, new_mode)
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Manual close
    # ------------------------------------------------------------------

    def manual_close(self, position_id: int) -> dict[str, Any]:
        """Close trade immediately via market order.  Also cancels working SL/TP."""
        from drift.brokers.ib_client import IBClient

        pos = self._trades.get_by_id(position_id)
        if not pos or pos.state not in ("WORKING", "FILLED"):
            return {"status": "error", "message": "Position not found or already closed."}

        client = IBClient(self._cfg.broker)

        if pos.state == "WORKING":
            if pos.parent_order_id:
                result = client.cancel_bracket(pos.parent_order_id)
            else:
                result = {"status": "ok"}
            self._trades.close_trade(position_id, "CLOSED_CANCEL", exit_reason="Operator cancelled before fill")
            return result

        # FILLED — close with market order
        result = client.close_position(pos.bias, pos.quantity)
        if result["status"] == "ok":
            self._trades.close_trade(
                position_id, "CLOSED_MANUAL",
                exit_price=result.get("fill_price"),
                exit_reason="Operator manual close",
            )
        return result

    # ------------------------------------------------------------------
    # Fill detection (call from polling loop)
    # ------------------------------------------------------------------

    def poll_positions(self) -> list[dict[str, Any]]:
        """Check IB for status updates on all active trades.

        Returns a list of state changes detected.
        """
        from drift.brokers.ib_client import IBClient

        changes: list[dict[str, Any]] = []
        active_trades = self._trades.get_active()
        if not active_trades:
            return changes

        client = IBClient(self._cfg.broker)

        for pos in active_trades:
            if pos.state == "WORKING" and pos.parent_order_id:
                result = client.check_order_status(pos.parent_order_id)
                if result.get("order_status") == "Filled":
                    fill_price = result.get("avg_fill_price") or pos.entry_limit
                    self._trades.mark_filled(pos.id, fill_price)
                    changes.append({
                        "type": "ENTRY_FILLED",
                        "position_id": pos.id,
                        "fill_price": fill_price,
                    })
                    log.info("Trade %d ENTRY FILLED at %.2f", pos.id, fill_price)

            elif pos.state == "FILLED":
                # Check SL
                if pos.sl_order_id:
                    sl_result = client.check_order_status(pos.sl_order_id)
                    if sl_result.get("order_status") == "Filled":
                        exit_price = sl_result.get("avg_fill_price") or pos.stop_loss
                        self._trades.close_trade(
                            pos.id, "CLOSED_SL", exit_price=exit_price,
                            exit_reason="Stop loss triggered",
                        )
                        changes.append({
                            "type": "SL_HIT",
                            "position_id": pos.id,
                            "exit_price": exit_price,
                        })
                        log.info("Trade %d STOP LOSS at %.2f", pos.id, exit_price)
                        continue

                # Check TP
                if pos.tp_order_id:
                    tp_result = client.check_order_status(pos.tp_order_id)
                    if tp_result.get("order_status") == "Filled":
                        exit_price = tp_result.get("avg_fill_price") or pos.active_tp
                        close_state = f"CLOSED_{pos.exit_mode}"
                        self._trades.close_trade(
                            pos.id, close_state, exit_price=exit_price,
                            exit_reason=f"{pos.exit_mode} target hit",
                        )
                        changes.append({
                            "type": f"{pos.exit_mode}_HIT",
                            "position_id": pos.id,
                            "exit_price": exit_price,
                        })
                        log.info("Trade %d %s HIT at %.2f", pos.id, pos.exit_mode, exit_price)

        return changes

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_open_positions(self):
        return self._trades.get_active()

    def get_filled_positions(self):
        return self._trades.get_filled()

    def has_open_position(self) -> bool:
        return self._trades.has_active_trade()

    def get_position(self, position_id: int):
        return self._trades.get_by_id(position_id)

    def get_position_history(self, limit: int = 50):
        return self._trades.get_history(limit)

    # ------------------------------------------------------------------
    # Assessment apply
    # ------------------------------------------------------------------

    def apply_assessment(self, position_id: int, rec) -> dict[str, Any]:
        """Apply an ``AssessmentRecommendation`` to an active trade.

        Handles all mutable parameters: SL, TP1, TP2, entry limit,
        hold window, and exit mode.  For CLOSE recommendations, delegates
        to ``manual_close``.

        Returns {"status": "ok", "changes": [...]} or error.
        """
        from drift.brokers.ib_client import IBClient

        pos = self._trades.get_by_id(position_id)
        if not pos or pos.state not in ("WORKING", "FILLED"):
            return {"status": "error", "message": "Position not found or already closed."}

        # CLOSE action — just close/cancel
        if rec.action == "CLOSE":
            return self.manual_close(position_id)

        changes: list[str] = []
        client = IBClient(self._cfg.broker)

        # ---- Stop loss ----
        if rec.new_stop_loss is not None and rec.new_stop_loss != pos.stop_loss:
            if pos.state == "FILLED" and pos.sl_order_id:
                result = client.modify_sl(
                    old_sl_order_id=pos.sl_order_id,
                    new_sl_price=rec.new_stop_loss,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                    quantity=pos.quantity,
                )
                if result["status"] != "ok":
                    return result
                self._trades.update_stop_loss(
                    position_id, rec.new_stop_loss,
                    sl_order_id=result.get("sl_order_id"),
                )
                changes.append(f"SL {pos.stop_loss:.2f} → {rec.new_stop_loss:.2f}")
            elif pos.state == "WORKING":
                # For WORKING orders, entry modification replaces the whole bracket
                # SL will be applied as part of entry modification below
                self._trades.update_stop_loss(position_id, rec.new_stop_loss)
                changes.append(f"SL {pos.stop_loss:.2f} → {rec.new_stop_loss:.2f}")

        # ---- Take profits ----
        tp_changed = False
        new_tp1 = rec.new_take_profit_1
        new_tp2 = rec.new_take_profit_2
        if new_tp1 is not None and new_tp1 != pos.take_profit_1:
            tp_changed = True
        if new_tp2 is not None and new_tp2 != pos.take_profit_2:
            tp_changed = True

        if tp_changed and pos.state == "FILLED":
            # If the active TP is the one being changed, modify on IB
            active_tp_changing = (
                (pos.exit_mode == "TP1" and new_tp1 is not None)
                or (pos.exit_mode == "TP2" and new_tp2 is not None)
            )
            if active_tp_changing and pos.tp_order_id:
                new_active = new_tp1 if pos.exit_mode == "TP1" else new_tp2
                result = client.modify_tp(
                    old_tp_order_id=pos.tp_order_id,
                    new_tp_price=new_active,
                    bias=pos.bias,
                    parent_order_id=pos.parent_order_id or 0,
                    quantity=pos.quantity,
                )
                if result["status"] != "ok":
                    return result
                self._trades.update_take_profits(
                    position_id, tp1=new_tp1, tp2=new_tp2,
                    tp_order_id=result.get("tp_order_id"),
                )
                self._trades.set_exit_mode(
                    position_id, pos.exit_mode, new_active,
                    tp_order_id=result.get("tp_order_id"),
                )
            else:
                # Not the active TP — just update the stored values
                self._trades.update_take_profits(position_id, tp1=new_tp1, tp2=new_tp2)

            if new_tp1 is not None:
                changes.append(f"TP1 {pos.take_profit_1:.2f} → {new_tp1:.2f}")
            if new_tp2 is not None:
                old_tp2 = f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—"
                changes.append(f"TP2 {old_tp2} → {new_tp2:.2f}")

        elif tp_changed and pos.state == "WORKING":
            # Stored only — bracket will be replaced if entry changes too
            self._trades.update_take_profits(position_id, tp1=new_tp1, tp2=new_tp2)
            if new_tp1 is not None:
                changes.append(f"TP1 {pos.take_profit_1:.2f} → {new_tp1:.2f}")
            if new_tp2 is not None:
                old_tp2 = f"{pos.take_profit_2:.2f}" if pos.take_profit_2 else "—"
                changes.append(f"TP2 {old_tp2} → {new_tp2:.2f}")

        # ---- Entry limit (WORKING only) ----
        if rec.new_entry_limit is not None and pos.state == "WORKING":
            if rec.new_entry_limit != pos.entry_limit:
                # Re-read pos to get any SL/TP changes already applied above
                updated_pos = self._trades.get_by_id(position_id)
                result = client.modify_entry(
                    old_parent_order_id=pos.parent_order_id or 0,
                    new_entry_price=rec.new_entry_limit,
                    bias=pos.bias,
                    stop_loss=updated_pos.stop_loss,
                    take_profit=updated_pos.take_profit_1,
                    quantity=pos.quantity,
                )
                if result["status"] != "ok":
                    return result
                self._trades.update_entry_limit(
                    position_id, rec.new_entry_limit,
                    parent_order_id=result.get("order_id"),
                    tp_order_id=result.get("tp_order_id"),
                    sl_order_id=result.get("sl_order_id"),
                    ib_perm_id=result.get("perm_id"),
                )
                old_entry = f"{pos.entry_limit:.2f}" if pos.entry_limit else "—"
                changes.append(f"Entry {old_entry} → {rec.new_entry_limit:.2f}")

        # ---- Hold window ----
        if rec.new_max_hold_minutes is not None and rec.new_max_hold_minutes != pos.max_hold_minutes:
            self._trades.update_hold_window(position_id, rec.new_max_hold_minutes)
            changes.append(f"Hold {pos.max_hold_minutes}m → {rec.new_max_hold_minutes}m")

        # ---- Exit mode ----
        if rec.recommended_exit_mode and rec.recommended_exit_mode != pos.exit_mode:
            if pos.state == "FILLED":
                mode_result = self.switch_exit_mode(position_id, rec.recommended_exit_mode)
                if mode_result["status"] != "ok":
                    return mode_result
                changes.append(f"Mode {pos.exit_mode} → {rec.recommended_exit_mode}")

        if not changes:
            changes.append("No parameter changes (HOLD)")

        log.info("Assessment applied to trade %d: %s", position_id, ", ".join(changes))
        return {"status": "ok", "changes": changes}

    def log_assessment(self, trade_id: int, rec) -> int:
        """Persist an assessment recommendation.  Returns the assessment id."""
        import json
        rec_json = json.dumps(rec.model_dump(), default=str)
        return self._trades.log_assessment(
            trade_id=trade_id,
            action=rec.action,
            confidence=rec.confidence,
            rationale=rec.rationale,
            recommendation_json=rec_json,
        )

    def dismiss_assessment(self, assessment_id: int) -> None:
        """Mark an assessment as dismissed (not applied)."""
        self._trades.mark_assessment_applied(assessment_id, applied=-1)

    def mark_assessment_applied(self, assessment_id: int) -> None:
        """Mark an assessment as applied."""
        self._trades.mark_assessment_applied(assessment_id, applied=1)

    def close(self) -> None:
        self._trades.close()
