from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from drift.config.models import SessionsSection
from drift.gates.base import Gate
from drift.models import GateResult, MarketSnapshot

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)  # Regular NYSE/CME session opens at 09:30 ET


class SessionGate(Gate):
    """Blocks signals outside configured active trading windows.

    Rules (evaluated in order):
        1. ``sessions.enabled`` is False → always passes
        2. ``snapshot.session`` is not ``'open'`` → blocked (pre/post/closed)
        3. Current ET time is within the first ``skip_first_n_minutes_after_open``
           minutes after 09:30 → blocked (high-spread open volatility)
        4. Current ET time falls within any configured block → passes
        5. Otherwise → blocked (outside all trading windows)

    Trading blocks in ``settings.yaml`` define *allowed* windows (e.g.
    09:40–11:30 and 13:30–15:30 ET). Anything outside them is rejected.
    """

    @property
    def name(self) -> str:
        return "session"

    def __init__(self, config: SessionsSection) -> None:
        self._config = config

    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:
        if not self._config.enabled:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Session gate disabled in config.",
            )

        # Accept both "open" (live provider) and "RTH" (replay provider / yfinance).
        if snapshot.session not in ("open", "RTH"):
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=f"Market not in regular session (status: '{snapshot.session}').",
            )

        # Use the snapshot timestamp so replay evaluates gates against the bar's
        # actual time, not the current wall-clock time.
        ref_dt = snapshot.as_of.astimezone(_ET)
        current_time = ref_dt.time().replace(second=0, microsecond=0)

        # Additional skip window guard: first N minutes after official open.
        # This is relevant when a block starts exactly at 09:30 — the field
        # lets operators add a buffer without changing the block definition.
        skip_n = self._config.skip_first_n_minutes_after_open
        if skip_n > 0:
            open_dt = datetime.combine(ref_dt.date(), _MARKET_OPEN, tzinfo=_ET)
            skip_until = (open_dt + timedelta(minutes=skip_n)).time()
            if _MARKET_OPEN <= current_time < skip_until:
                return GateResult(
                    gate_name=self.name,
                    passed=False,
                    reason=(
                        f"Within {skip_n}-min skip window after market open "
                        f"(09:30 ET) — high spread / erratic prints."
                    ),
                )

        # Check against allowed trading blocks.
        for block in self._config.blocks:
            block_start = time.fromisoformat(block.start)
            block_end = time.fromisoformat(block.end)
            if block_start <= current_time <= block_end:
                return GateResult(
                    gate_name=self.name,
                    passed=True,
                    reason=f"Within active window {block.start}–{block.end} ET.",
                )

        time_str = current_time.strftime("%H:%M")
        return GateResult(
            gate_name=self.name,
            passed=False,
            reason=f"Outside all active trading windows (current ET time: {time_str}).",
        )
