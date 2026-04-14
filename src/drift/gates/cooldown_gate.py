from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from drift.config.models import GatesSection, RiskSection
from drift.gates.base import Gate
from drift.models import GateResult, MarketSnapshot

# Outcomes that represent an actual signal attempt (not a gate block).
# These are the events that count toward the cooldown timer.
_SIGNAL_OUTCOMES = {"SNAPSHOT_ONLY", "LLM_NO_TRADE", "TRADE_PLAN_ISSUED"}


class CooldownGate(Gate):
    """Blocks a new signal if one was generated too recently.

    Reads the JSONL event log and scans for the most recent event whose
    ``final_outcome`` was a genuine signal attempt (not a gate block). If that
    event occurred within ``risk.cooldown_minutes``, the gate blocks.

    Gate-blocked events (``final_outcome == "BLOCKED"``) do not count — only
    actual signal cycles advance the cooldown timer.
    """

    @property
    def name(self) -> str:
        return "cooldown"

    def __init__(
        self,
        gates_config: GatesSection,
        risk_config: RiskSection,
        log_path: str | Path,
    ) -> None:
        self._gates = gates_config
        self._cooldown_minutes = risk_config.cooldown_minutes
        self._log_path = Path(log_path)

    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:  # noqa: ARG002
        if not self._gates.cooldown_enabled:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Cooldown gate disabled in config.",
            )

        if self._cooldown_minutes == 0:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Cooldown period is 0 minutes.",
            )

        last_signal_time = self._last_signal_time()

        if last_signal_time is None:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="No previous signal cycles found in event log.",
            )

        now = datetime.now(tz=timezone.utc)
        elapsed_minutes = (now - last_signal_time).total_seconds() / 60

        if elapsed_minutes < self._cooldown_minutes:
            remaining = round(self._cooldown_minutes - elapsed_minutes, 1)
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=(
                    f"Cooldown active — {remaining} min remaining "
                    f"(cooldown window: {self._cooldown_minutes} min)."
                ),
            )

        return GateResult(
            gate_name=self.name,
            passed=True,
            reason=f"Cooldown clear — last signal cycle was {round(elapsed_minutes, 1)} min ago.",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _last_signal_time(self) -> datetime | None:
        """Scan the JSONL log for the most recent signal-cycle timestamp."""
        if not self._log_path.exists():
            return None

        last: datetime | None = None
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("final_outcome") not in _SIGNAL_OUTCOMES:
                continue

            try:
                ts = datetime.fromisoformat(event["event_time"])
                # Ensure timezone-aware for comparison.
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if last is None or ts > last:
                    last = ts
            except (KeyError, ValueError):
                continue

        return last
