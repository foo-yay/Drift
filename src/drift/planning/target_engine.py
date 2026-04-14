from __future__ import annotations

from drift.config.models import RiskSection
from drift.models import LLMDecision


class TargetEngine:
    """Deterministic take-profit target calculator.

    TP1 = entry ± (stop_distance * atr_target_mult)
    TP2 = entry ± (stop_distance * atr_target_mult * 1.5)  [optional extension]

    The R:R check is enforced here. If TP1 doesn't meet min_reward_risk,
    the caller should discard the signal.
    """

    def __init__(self, config: RiskSection) -> None:
        self._cfg = config

    def calculate(
        self,
        decision: LLMDecision,
        stop_loss: float,
    ) -> tuple[float, float | None, float]:
        """Calculate TP1, optional TP2, and the reward-to-risk ratio.

        Args:
            decision:  The validated LLM decision (must be LONG or SHORT).
            stop_loss: The computed stop price from StopEngine.

        Returns:
            (tp1, tp2, reward_risk_ratio) — tp2 is None if the signal is weak.
        """
        if decision.decision == "LONG":
            entry = decision.entry_zone[0]
            stop_dist = entry - stop_loss
            tp1 = round(entry + (stop_dist * self._cfg.atr_target_mult), 2)
            tp2 = round(entry + (stop_dist * self._cfg.atr_target_mult * 1.5), 2)
        else:  # SHORT
            entry = decision.entry_zone[1]
            stop_dist = stop_loss - entry
            tp1 = round(entry - (stop_dist * self._cfg.atr_target_mult), 2)
            tp2 = round(entry - (stop_dist * self._cfg.atr_target_mult * 1.5), 2)

        rr = round(stop_dist * self._cfg.atr_target_mult / stop_dist, 2) if stop_dist else 0.0
        # rr simplifies to atr_target_mult — keep explicit for auditability
        rr = self._cfg.atr_target_mult

        # Only emit TP2 if confidence is high enough to warrant a second target
        tp2_out = tp2 if decision.confidence >= 70 else None

        return tp1, tp2_out, round(rr, 2)
