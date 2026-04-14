from __future__ import annotations

from pathlib import Path

from drift.config.models import GatesSection
from drift.gates.base import Gate
from drift.models import GateResult, MarketSnapshot


class KillSwitchGate(Gate):
    """Blocks all signals when the operator kill-switch file is present.

    The kill switch is a plain sentinel file on disk. Its presence means HALT;
    its absence means trading is permitted. Toggle it with the CLI:

        drift kill       # creates the file → all signals blocked
        drift resume     # removes the file → signals re-enabled

    The gate re-checks the file on every evaluate() call so the operator can
    toggle the switch without restarting the process.
    """

    @property
    def name(self) -> str:
        return "kill_switch"

    def __init__(self, config: GatesSection) -> None:
        self._config = config
        self._path = Path(config.kill_switch_path)

    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:  # noqa: ARG002
        if not self._config.kill_switch_enabled:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Kill-switch gate disabled in config.",
            )

        if self._path.exists():
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=f"Kill switch is ACTIVE. Run 'drift resume' to re-enable signals.",
            )

        return GateResult(
            gate_name=self.name,
            passed=True,
            reason="Kill switch not active.",
        )
