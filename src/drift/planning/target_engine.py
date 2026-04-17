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
        nat = decision.natural_target_price

        if decision.decision == "LONG":
            entry_worst = decision.entry_zone[1]  # entry_max — worst-case fill for a LONG
            stop_dist = entry_worst - stop_loss
            tp1 = round(entry_worst + (stop_dist * self._cfg.atr_target_mult), 2)
            tp2 = round(entry_worst + (stop_dist * self._cfg.atr_target_mult * 1.5), 2)
            # Cap TP1 at the structural target when provided (e.g. VWAP for mean_reversion,
            # opposite range boundary for range_fade). Suppress TP2 — there is no
            # extension target beyond a known structural ceiling.
            if nat is not None and nat > entry_worst:
                tp1 = round(min(tp1, nat), 2)
                tp2 = None
        else:  # SHORT
            entry_worst = decision.entry_zone[0]  # entry_min — worst-case fill for a SHORT
            stop_dist = stop_loss - entry_worst
            tp1 = round(entry_worst - (stop_dist * self._cfg.atr_target_mult), 2)
            tp2 = round(entry_worst - (stop_dist * self._cfg.atr_target_mult * 1.5), 2)
            if nat is not None and nat < entry_worst:
                tp1 = round(max(tp1, nat), 2)
                tp2 = None

        rr = round(stop_dist * self._cfg.atr_target_mult / stop_dist, 2) if stop_dist else 0.0
        # rr simplifies to atr_target_mult — keep explicit for auditability
        rr = self._cfg.atr_target_mult

        # Only emit TP2 if confidence is high enough and no structural ceiling was applied
        tp2_out = tp2 if (tp2 is not None and decision.confidence >= 70) else None

        return tp1, tp2_out, round(rr, 2)
