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

    For **TRADE_PLAN_ISSUED** events: if a ``db_path`` is provided, the gate
    checks whether the trade is still active (WORKING/FILLED).  If the trade
    has been closed/cancelled, the cooldown clears immediately — no need to
    wait for the full timer.  When no DB path is available (replay mode), it
    falls back to the timestamp-based JSONL approach.

    For **LLM_NO_TRADE** events: cooldown is ``risk.no_trade_cooldown_minutes``
    measured from the event timestamp.

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
        db_path: str | Path | None = None,
    ) -> None:
        self._gates = gates_config
        self._cooldown_minutes = risk_config.cooldown_minutes
        self._no_trade_cooldown_minutes = getattr(
            risk_config, "no_trade_cooldown_minutes", risk_config.cooldown_minutes,
        )
        self._log_path = Path(log_path)
        self._db_path = db_path

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

        # ---- Trade-based cooldown (preferred when DB available) ----
        # If there's an active trade in the DB, block new cycles.
        if self._db_path is not None:
            active_result = self._check_active_trade()
            if active_result is not None:
                return active_result

        # ---- JSONL-based cooldown (NO_TRADE events + fallback for trades) ----
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

        # Check active trade first (DB-based)
        if self._db_path is not None:
            try:
                from drift.storage.trade_store import TradeStore
                store = TradeStore(self._db_path)
                active = store.get_active()
                store.close()
                if active:
                    pos = active[0]
                    anchor_str = pos.thesis_anchor or pos.generated_at
                    if anchor_str:
                        anchor_dt = datetime.fromisoformat(anchor_str)
                        if anchor_dt.tzinfo is None:
                            anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
                        remaining = pos.max_hold_minutes * 60 - (
                            datetime.now(tz=timezone.utc) - anchor_dt
                        ).total_seconds()
                        return remaining if remaining > 0 else None
            except Exception:  # noqa: BLE001
                pass

        # JSONL fallback
        last_time, hold_minutes = self._get_last_signal()
        if last_time is None:
            return None

        elapsed = (datetime.now(tz=timezone.utc) - last_time).total_seconds()
        remaining = hold_minutes * 60 - elapsed
        return remaining if remaining > 0 else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_active_trade(self) -> GateResult | None:
        """If an active trade exists, block the cycle.  Returns None if no active trade."""
        try:
            from drift.storage.trade_store import TradeStore
            store = TradeStore(self._db_path)
            active = store.get_active()
            store.close()
        except Exception:  # noqa: BLE001
            return None

        if not active:
            return None

        pos = active[0]
        anchor_str = pos.thesis_anchor or pos.generated_at
        if anchor_str:
            try:
                anchor_dt = datetime.fromisoformat(anchor_str)
                if anchor_dt.tzinfo is None:
                    anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(tz=timezone.utc) - anchor_dt).total_seconds() / 60
                remaining = round(pos.max_hold_minutes - elapsed, 1)
                if remaining > 0:
                    return GateResult(
                        gate_name=self.name,
                        passed=False,
                        reason=(
                            f"Active {pos.state} trade #{pos.id} — "
                            f"{remaining} min remaining in thesis window "
                            f"({pos.max_hold_minutes} min)."
                        ),
                    )
            except (ValueError, TypeError):
                pass

        # Active trade but can't compute remaining time — still block
        return GateResult(
            gate_name=self.name,
            passed=False,
            reason=f"Active {pos.state} trade #{pos.id} — cooldown active.",
        )

    def _get_last_signal(self) -> tuple[datetime | None, int]:
        """Scan the JSONL log for the most recent LLM-reaching cycle.

        Returns ``(timestamp, cooldown_minutes)``.

        When ``db_path`` is set, only ``LLM_NO_TRADE`` events are considered
        here — ``TRADE_PLAN_ISSUED`` cooldowns are handled by
        ``_check_active_trade`` which reads the trade database directly.

        For ``LLM_NO_TRADE`` events, it uses ``risk.no_trade_cooldown_minutes``.
        For ``TRADE_PLAN_ISSUED`` (no-DB fallback), cooldown uses
        ``trade_plan.max_hold_minutes`` (falls back to ``risk.cooldown_minutes``).
        """
        if not self._log_path.exists():
            return None, self._cooldown_minutes

        # When DB is available, only look at NO_TRADE events in the JSONL
        # (TRADE_PLAN cooldowns are handled by _check_active_trade)
        outcomes_to_check = (
            _NO_TRADE_OUTCOMES if self._db_path is not None
            else _COOLDOWN_OUTCOMES
        )

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
            if outcome not in outcomes_to_check:
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
