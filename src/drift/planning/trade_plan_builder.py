from __future__ import annotations

import logging
from datetime import datetime, timezone

from drift.config.models import AppConfig
from drift.models import LLMDecision, MarketSnapshot, TradePlan
from drift.planning.stop_engine import StopEngine
from drift.planning.target_engine import TargetEngine

logger = logging.getLogger(__name__)


class TradePlanBuilder:
    """Constructs a validated TradePlan from an LLMDecision and MarketSnapshot.

    Returns None if any hard constraint is violated (bad stop, R:R too low,
    confidence below threshold, direction not allowed).
    """

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        strat = config.strategy
        self._stop_engine = StopEngine(config.risk, structure_buffer=strat.structure_buffer_points)
        self._target_engine = TargetEngine(config.risk)
        self._chase_buffer = strat.chase_buffer_points

    def build(
        self,
        snapshot: MarketSnapshot,
        decision: LLMDecision,
    ) -> TradePlan | None:
        """Attempt to build a TradePlan.

        Returns None (NO_TRADE) if any hard gate fails.
        """
        # Direction allowed?
        if decision.decision == "LONG" and not self._cfg.instrument.allow_long:
            logger.info("LONG decision rejected — allow_long is false")
            return None
        if decision.decision == "SHORT" and not self._cfg.instrument.allow_short:
            logger.info("SHORT decision rejected — allow_short is false")
            return None

        # Confidence threshold
        if decision.confidence < self._cfg.risk.min_confidence:
            logger.info(
                "Decision rejected — confidence %d < min %d",
                decision.confidence,
                self._cfg.risk.min_confidence,
            )
            return None

        # Setup type allowed?
        if decision.setup_type not in self._cfg.strategy.allowed_setup_types:
            logger.info("Decision rejected — setup_type %r not in allowed list", decision.setup_type)
            return None

        # Volume imbalance proxy (DOM substitute) — directional check.
        # An up-bar-weighted imbalance score > 0 means buyers dominate; < 0 means
        # sellers dominate.  Block the trade when opposing pressure is strong enough
        # to call the directional edge into question.
        if self._cfg.gates.volume_imbalance_gate_enabled:
            imbalance = snapshot.volume_imbalance
            threshold = self._cfg.gates.volume_imbalance_threshold
            if imbalance is not None:
                if decision.decision == "LONG" and imbalance < -threshold:
                    logger.info(
                        "LONG rejected — volume imbalance %.1f below -%s (seller pressure)",
                        imbalance,
                        threshold,
                    )
                    return None
                if decision.decision == "SHORT" and imbalance > threshold:
                    logger.info(
                        "SHORT rejected — volume imbalance %.1f above +%s (buyer pressure)",
                        imbalance,
                        threshold,
                    )
                    return None

        # ATR for stop calculation (fall back to 10 pts if missing from snapshot)
        atr = self._resolve_atr(snapshot)

        # Stop loss
        stop_loss = self._stop_engine.calculate(snapshot, decision, atr)
        if stop_loss is None:
            logger.info("Decision rejected — stop engine could not produce a valid stop")
            return None

        # Targets + R:R
        tp1, tp2, rr = self._target_engine.calculate(decision, stop_loss)

        if rr < self._cfg.risk.min_reward_risk:
            logger.info("Decision rejected — R:R %.2f < min %.2f", rr, self._cfg.risk.min_reward_risk)
            return None

        # Chase level — how far beyond entry_max (long) or entry_min (short) the
        # operator should not chase
        if decision.decision == "LONG":
            chase = round(decision.entry_zone[1] + self._chase_buffer, 2)
        else:
            chase = round(decision.entry_zone[0] - self._chase_buffer, 2)

        operator_instructions = self._build_instructions(decision, stop_loss, tp1, chase)

        return TradePlan(
            generated_at=datetime.now(tz=timezone.utc),
            symbol=snapshot.symbol,
            bias=decision.decision,  # type: ignore[arg-type]
            setup_type=decision.setup_type,
            confidence=decision.confidence,
            entry_min=decision.entry_zone[0],
            entry_max=decision.entry_zone[1],
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            reward_risk_ratio=rr,
            max_hold_minutes=decision.hold_minutes or self._cfg.risk.max_hold_minutes_default,
            thesis=decision.thesis,
            invalidation_conditions=[decision.invalidation_hint],
            operator_instructions=operator_instructions,
            do_not_trade_if=decision.do_not_trade_if,
            chase_above_below=chase,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_atr(self, snapshot: MarketSnapshot) -> float:
        """Pull ATR from snapshot; fall back to 10 pts if not yet computed."""
        if snapshot.atr is not None and snapshot.atr > 0:
            return snapshot.atr
        return 10.0

    def _build_instructions(
        self,
        decision: LLMDecision,
        stop_loss: float,
        tp1: float,
        chase_level: float,
    ) -> list[str]:
        direction = decision.decision
        entry_low, entry_high = decision.entry_zone
        action = "BUY" if direction == "LONG" else "SELL"
        close_action = "SELL" if direction == "LONG" else "BUY"
        price_dir = "drops into" if direction == "LONG" else "rallies into"
        chase_warn = f"above {chase_level:.2f}" if direction == "LONG" else f"below {chase_level:.2f}"

        return [
            f"① ENTRY — Place a {action} LIMIT order at any price inside {entry_low:.2f} – {entry_high:.2f} · Qty: 1 contract · Time in force: Day.",
            f"   Do NOT use Market order. If price is already {chase_warn} before you submit — cancel and skip this trade.",
            f"② STOP LOSS — Immediately after your entry fills: place a {close_action} STOP order at {stop_loss:.2f} · Qty: 1 · Time in force: GTC.",
            f"   (Robinhood: tap the position → {close_action} → Order type: Stop → Stop price: {stop_loss:.2f})",
            f"③ TAKE PROFIT — Place a {close_action} LIMIT order at {tp1:.2f} · Qty: 1 · Time in force: GTC.",
            f"   (Robinhood: tap the position → {close_action} → Order type: Limit → Limit price: {tp1:.2f})",
            f"④ TIME STOP — If neither order fills within {decision.hold_minutes} min: cancel both open orders, then place a {close_action} MARKET order to close.",
        ]
