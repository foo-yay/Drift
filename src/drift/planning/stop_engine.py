from __future__ import annotations
import logging

from drift.config.models import RiskSection
from drift.models import LLMDecision, MarketSnapshot

logger = logging.getLogger(__name__)


class StopEngine:
    """Deterministic stop-loss calculator.

    Stop placement hierarchy (first rule that produces a valid stop wins):
    1. Structural stop — use the nearest significant swing extreme beyond the
       entry zone, padded by structure_buffer_points.
    2. ATR floor — if the structural stop would be too tight, enforce a
       minimum of atr * atr_stop_floor_mult.
    3. Minimum clamp — if the result is still below min_stop_points, widen
       to exactly min_stop_points (never reject for being too tight).
    4. Hard cap — reject only if the stop exceeds max_stop_points.
    """

    def __init__(self, config: RiskSection, structure_buffer: float = 2.0) -> None:
        self._cfg = config
        self._buffer = structure_buffer

    def calculate(
        self,
        snapshot: MarketSnapshot,
        decision: LLMDecision,
        atr: float,
    ) -> float | None:
        """Calculate a stop-loss price.

        Returns:
            The stop price, or None if no valid stop can be constructed
            (caller should treat as NO_TRADE / discard signal).
        """
        if decision.decision == "LONG":
            return self._long_stop(snapshot, decision, atr)
        if decision.decision == "SHORT":
            return self._short_stop(snapshot, decision, atr)
        return None

    # ------------------------------------------------------------------
    # Directional helpers
    # ------------------------------------------------------------------

    def _long_stop(
        self, snapshot: MarketSnapshot, decision: LLMDecision, atr: float
    ) -> float | None:
        entry_min = decision.entry_zone[0]
        atr_floor = entry_min - (atr * self._cfg.atr_stop_floor_mult)

        # Prefer LLM-identified structural invalidation price over ATR floor.
        # Buffer it by structure_buffer_points so the stop sits just beyond
        # the level rather than exactly on it.
        inv = decision.invalidation_price
        if inv is not None and inv < entry_min:
            structural = inv - self._buffer
            structural_dist = entry_min - structural
            if structural_dist <= self._cfg.max_stop_points:
                stop = structural
                logger.info(
                    "LONG stop: structural inv %.2f → stop %.2f (%.0f pts)",
                    inv, stop, structural_dist,
                )
            else:
                stop = atr_floor - self._buffer
                logger.info(
                    "LONG stop: structural inv %.2f too wide (%.0f pts > max %.0f), using ATR floor %.2f",
                    inv, structural_dist, self._cfg.max_stop_points, stop,
                )
        else:
            stop = atr_floor - self._buffer

        return self._validate_stop(entry_min, stop, direction="LONG")

    def _short_stop(
        self, snapshot: MarketSnapshot, decision: LLMDecision, atr: float
    ) -> float | None:
        entry_max = decision.entry_zone[1]
        atr_floor = entry_max + (atr * self._cfg.atr_stop_floor_mult)

        inv = decision.invalidation_price
        if inv is not None and inv > entry_max:
            structural = inv + self._buffer
            structural_dist = structural - entry_max
            if structural_dist <= self._cfg.max_stop_points:
                stop = structural
                logger.info(
                    "SHORT stop: structural inv %.2f → stop %.2f (%.0f pts)",
                    inv, stop, structural_dist,
                )
            else:
                stop = atr_floor + self._buffer
                logger.info(
                    "SHORT stop: structural inv %.2f too wide (%.0f pts > max %.0f), using ATR floor %.2f",
                    inv, structural_dist, self._cfg.max_stop_points, stop,
                )
        else:
            stop = atr_floor + self._buffer

        return self._validate_stop(entry_max, stop, direction="SHORT")

    def _validate_stop(
        self, entry: float, stop: float, direction: str
    ) -> float | None:
        stop_distance = abs(entry - stop)

        if stop_distance < self._cfg.min_stop_points:
            logger.info(
                "Stop clamped (%s) — ATR-based distance %.2f pts below min %.2f; widening to min",
                direction, stop_distance, self._cfg.min_stop_points,
            )
            stop_distance = self._cfg.min_stop_points
            stop = (
                round(entry - stop_distance, 2)
                if direction == "LONG"
                else round(entry + stop_distance, 2)
            )

        if stop_distance > self._cfg.max_stop_points:
            logger.info(
                "Stop rejected (%s) — distance %.2f pts exceeds max_stop_points %.2f",
                direction, stop_distance, self._cfg.max_stop_points,
            )
            return None  # too wide

        return round(stop, 2)
