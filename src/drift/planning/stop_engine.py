from __future__ import annotations

from drift.config.models import RiskSection
from drift.models import LLMDecision, MarketSnapshot


class StopEngine:
    """Deterministic stop-loss calculator.

    Stop placement hierarchy (first rule that produces a valid stop wins):
    1. Structural stop — use the nearest significant swing extreme beyond the
       entry zone, padded by structure_buffer_points.
    2. ATR floor — if the structural stop would be too tight, enforce a
       minimum of atr * atr_stop_floor_mult.
    3. Hard cap — reject if the resulting stop exceeds max_stop_points.
    4. Hard floor — reject if the resulting stop is below min_stop_points.
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

        # Start with the ATR floor
        atr_floor = entry_min - (atr * self._cfg.atr_stop_floor_mult)

        # If the LLM provided an invalidation hint referencing a price below
        # entry, we treat entry_zone[0] - buffer as a structural reference.
        # In the absence of structural swing data on the snapshot, we use the
        # ATR floor directly.
        stop = atr_floor - self._buffer

        return self._validate_stop(entry_min, stop, direction="LONG")

    def _short_stop(
        self, snapshot: MarketSnapshot, decision: LLMDecision, atr: float
    ) -> float | None:
        entry_max = decision.entry_zone[1]
        atr_floor = entry_max + (atr * self._cfg.atr_stop_floor_mult)
        stop = atr_floor + self._buffer

        return self._validate_stop(entry_max, stop, direction="SHORT")

    def _validate_stop(
        self, entry: float, stop: float, direction: str
    ) -> float | None:
        stop_distance = abs(entry - stop)

        if stop_distance < self._cfg.min_stop_points:
            return None  # too tight
        if stop_distance > self._cfg.max_stop_points:
            return None  # too wide

        return round(stop, 2)
