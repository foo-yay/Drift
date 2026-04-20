from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from drift.config.models import GatesSection, RiskSection
from drift.gates.base import Gate
from drift.models import GateResult, MarketSnapshot

# Outcomes that start a cooldown period.
# TRADE_PLAN_ISSUED uses the trade plan's max_hold_minutes (or risk.cooldown_minutes fallback).
# LLM_NO_TRADE uses the separate risk.no_trade_cooldown_minutes window.
_TRADE_OUTCOMES = {"TRADE_PLAN_ISSUED"}
_NO_TRADE_OUTCOMES = {"LLM_NO_TRADE"}
_COOLDOWN_OUTCOMES = _TRADE_OUTCOMES | _NO_TRADE_OUTCOMES


class CooldownGate(Gate):
    """Blocks a new cycle if a recent LLM call was made.

    Reads the JSONL event log and scans for the most recent event whose
    ``final_outcome`` was either ``TRADE_PLAN_ISSUED`` or ``LLM_NO_TRADE``.

    * After a **TRADE_PLAN_ISSUED**, the cooldown window is the trade plan's
      ``max_hold_minutes`` (falling back to ``risk.cooldown_minutes``).
    * After an **LLM_NO_TRADE**, the cooldown is ``risk.no_trade_cooldown_minutes``
      (default 15 min) — prevents repeated LLM calls when gates pass but the
      LLM sees nothing actionable.

    Gate-blocked events (``final_outcome == "BLOCKED"``) do not count — only
    outcomes that reached the LLM advance the cooldown timer.
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
        self._no_trade_cooldown_minutes = getattr(
            risk_config, "no_trade_cooldown_minutes", risk_config.cooldown_minutes,
        )
        self._log_path = Path(log_path)

    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:  # noqa: ARG002
        if not self._gates.cooldown_enabled:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Cooldown gate disabled in config.",
            )

        if self._cooldown_minutes == 0 and self._no_trade_cooldown_minutes == 0:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Cooldown periods are 0 minutes.",
            )

        last_signal_time, cooldown_minutes = self._get_last_signal()

        if last_signal_time is None:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="No previous LLM cycles found in event log.",
            )

        now = datetime.now(tz=timezone.utc)
        elapsed_minutes = (now - last_signal_time).total_seconds() / 60

        if elapsed_minutes < cooldown_minutes:
            remaining = round(cooldown_minutes - elapsed_minutes, 1)
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=(
                    f"Cooldown active — {remaining} min remaining "
                    f"(cooldown window: {cooldown_minutes} min)."
                ),
            )

        return GateResult(
            gate_name=self.name,
            passed=True,
            reason=f"Cooldown clear — last LLM cycle was {round(elapsed_minutes, 1)} min ago.",
        )

    def seconds_remaining(self) -> float | None:
        """Seconds until cooldown clears, or ``None`` if cooldown is not active.

        Used by the scheduler to schedule a one-shot wakeup cycle the moment
        the cooldown window expires, rather than waiting up to a full loop
        interval.
        """
        if not self._gates.cooldown_enabled or self._cooldown_minutes == 0:
            return None

        last_time, hold_minutes = self._get_last_signal()
        if last_time is None:
            return None

        elapsed = (datetime.now(tz=timezone.utc) - last_time).total_seconds()
        remaining = hold_minutes * 60 - elapsed
        return remaining if remaining > 0 else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_last_signal(self) -> tuple[datetime | None, int]:
        """Scan the JSONL log for the most recent LLM-reaching cycle.

        Returns ``(timestamp, cooldown_minutes)``.

        For ``TRADE_PLAN_ISSUED`` events, ``cooldown_minutes`` is drawn from
        ``trade_plan.max_hold_minutes`` (falls back to ``risk.cooldown_minutes``).
        For ``LLM_NO_TRADE`` events, it uses ``risk.no_trade_cooldown_minutes``.
        """
        if not self._log_path.exists():
            return None, self._cooldown_minutes

        last_time: datetime | None = None
        last_cooldown: int = self._cooldown_minutes
        try:
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None, self._cooldown_minutes

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            outcome = event.get("final_outcome")
            if outcome not in _COOLDOWN_OUTCOMES:
                continue

            try:
                ts = datetime.fromisoformat(event["event_time"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if last_time is None or ts > last_time:
                    last_time = ts
                    if outcome in _TRADE_OUTCOMES:
                        raw = (event.get("trade_plan") or {}).get("max_hold_minutes")
                        last_cooldown = int(raw) if raw and int(raw) > 0 else self._cooldown_minutes
                    else:
                        last_cooldown = self._no_trade_cooldown_minutes
            except (KeyError, ValueError):
                continue

        return last_time, last_cooldown
