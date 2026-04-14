from __future__ import annotations

from drift.gates.base import Gate
from drift.models import GateReport, GateResult, MarketSnapshot


class GateRunner:
    """Evaluates an ordered sequence of gates against a MarketSnapshot.

    Gates are evaluated in the order provided. The runner short-circuits on
    the first failure — remaining gates are skipped to avoid unnecessary work
    and to surface only the actionable blocking reason.

    Returns a GateReport containing:
      - ``all_passed``: True only if every gate in the sequence passed
      - ``results``: evaluated gate results (may be fewer than total gates
        if the runner short-circuited)

    Typical gate order (cheapest / most common blockers first):
        KillSwitchGate → SessionGate → CalendarGate → RegimeGate → CooldownGate
    """

    def __init__(self, gates: list[Gate]) -> None:
        self._gates = gates

    def run(self, snapshot: MarketSnapshot) -> GateReport:
        results: list[GateResult] = []

        for gate in self._gates:
            result = gate.evaluate(snapshot)
            results.append(result)
            if not result.passed:
                break  # short-circuit; do not evaluate remaining gates

        all_passed = all(r.passed for r in results) and len(results) == len(self._gates)
        return GateReport(all_passed=all_passed, results=results)
