from __future__ import annotations

from abc import ABC, abstractmethod

from drift.models import GateResult, MarketSnapshot


class Gate(ABC):
    """Base class for all deterministic gate checks.

    Each gate receives a MarketSnapshot and returns a GateResult indicating
    whether the signal is allowed to proceed. Gates are evaluated in sequence
    by the gate runner; the first failure short-circuits the remainder.

    Implementing a new gate:
        1. Create a new file in gates/ (e.g. gates/session_gate.py)
        2. Subclass Gate and implement evaluate()
        3. Register it in GateRunner (gates/runner.py, to be built)
        4. Add at least one pass/fail test in tests/
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier shown in gate reports and logs."""

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:
        """Evaluate whether this gate passes for the given snapshot.

        Args:
            snapshot: The current MarketSnapshot produced by FeatureEngine.

        Returns:
            GateResult with passed=True (allowed) or passed=False (blocked),
            and a human-readable reason.
        """
