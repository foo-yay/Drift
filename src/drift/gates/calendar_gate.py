from __future__ import annotations

from datetime import datetime, timezone

from drift.config.models import CalendarSection
from drift.gates.base import Gate
from drift.gates.calendar_provider import ForexFactoryCalendarProvider
from drift.models import CalendarEvent, GateResult, MarketSnapshot


class CalendarGate(Gate):
    """Blocks signals when a high-impact economic event is scheduled within
    the configured buffer window.

    Logic:
        - Fetch this week's events from ForexFactory (cached hourly)
        - Filter to HIGH impact events matching the configured countries
        - If any such event is within `buffer_minutes_before` of its start
          time, or within `buffer_minutes_after` of having already started,
          the gate fails with a descriptive reason
        - If the calendar section is disabled in config, the gate always passes
        - If the feed is unavailable, the gate passes (safe degradation)

    Example blocked output:
        GateResult(gate_name="calendar", passed=False,
                   reason="HIGH impact event in 8 min: CPI m/m (USD) at 08:30 ET")
    """

    @property
    def name(self) -> str:
        return "calendar"

    def __init__(self, config: CalendarSection) -> None:
        self._config = config
        self._provider = ForexFactoryCalendarProvider(
            cache_ttl_minutes=config.cache_ttl_minutes
        )

    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:  # noqa: ARG002
        if not self._config.enabled:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Calendar gate disabled in config.",
            )

        now = datetime.now(tz=timezone.utc)
        events = self._provider.get_events()

        blocking_event, minutes = self._find_blocking_event(events, now)

        if blocking_event is None:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="No high-impact events within the buffer window.",
            )

        direction = "in" if minutes > 0 else "ago"
        abs_min = abs(round(minutes, 1))
        return GateResult(
            gate_name=self.name,
            passed=False,
            reason=(
                f"HIGH impact event {abs_min} min {direction}: "
                f"{blocking_event.title} ({blocking_event.country}) "
                f"at {blocking_event.event_time.astimezone().strftime('%H:%M %Z')}"
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_blocking_event(
        self, events: list[CalendarEvent], now: datetime
    ) -> tuple[CalendarEvent | None, float]:
        """Return the first blocking event and its signed minutes-until value."""
        countries = {c.upper() for c in self._config.filter_countries}

        for event in events:
            if not event.is_high_impact:
                continue
            if event.country.upper() not in countries:
                continue

            minutes_until = event.minutes_until(now)

            # Block if event is approaching (within buffer_before)
            if 0 < minutes_until <= self._config.buffer_minutes_before:
                return event, minutes_until

            # Block if event just started (within buffer_after)
            if -self._config.buffer_minutes_after <= minutes_until <= 0:
                return event, minutes_until

        return None, 0.0
