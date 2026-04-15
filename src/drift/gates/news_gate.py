from __future__ import annotations

from datetime import datetime, timezone

from drift.config.models import GatesSection
from drift.gates.base import Gate
from drift.gates.calendar_provider import ForexFactoryCalendarProvider
from drift.models import CalendarEvent, GateResult, MarketSnapshot


class NewsGate(Gate):
    """Blocks signals during a symmetric blackout window around HIGH-impact
    macro news events (Forex Factory calendar, USD events).

    Unlike :class:`CalendarGate` (which has separate before/after buffers and
    a full ``CalendarSection`` config), this gate uses a single
    ``news_blackout_minutes`` value applied symmetrically — N minutes before
    the event and N minutes after.  It is toggled via
    ``GatesSection.news_gate_enabled``.

    Shares the same ForexFactory feed as CalendarGate; the disk cache ensures
    both gates pay only one HTTP round-trip per TTL window even when running
    in the same process.

    Safe degradation: if the feed is unavailable and no disk cache exists, the
    gate passes so a network hiccup never silences all signals.

    Example blocked reason:
        "NEWS blackout: NFP (USD) starts in 12.0 min (window=30 min)"
    """

    _USD_COUNTRIES = {"USD"}

    @property
    def name(self) -> str:
        return "news"

    def __init__(self, config: GatesSection) -> None:
        self._config = config
        # 60-minute in-process TTL; disk cache handles cross-process dedup.
        self._provider = ForexFactoryCalendarProvider(cache_ttl_minutes=60)

    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:  # noqa: ARG002
        if not self._config.news_gate_enabled:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="News gate disabled in config.",
            )

        now = datetime.now(tz=timezone.utc)
        events = self._provider.get_events()

        blocking_event, minutes_until = self._find_blocking_event(events, now)

        if blocking_event is None:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="No high-impact news events within the blackout window.",
            )

        window = self._config.news_blackout_minutes
        if minutes_until > 0:
            direction = f"starts in {round(minutes_until, 1)} min"
        else:
            direction = f"started {round(abs(minutes_until), 1)} min ago"

        return GateResult(
            gate_name=self.name,
            passed=False,
            reason=(
                f"NEWS blackout: {blocking_event.title} ({blocking_event.country}) "
                f"{direction} (window={window} min)"
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_blocking_event(
        self, events: list[CalendarEvent], now: datetime
    ) -> tuple[CalendarEvent | None, float]:
        """Return the first blocking USD HIGH-impact event and its signed minutes-until."""
        window = self._config.news_blackout_minutes

        for event in events:
            if not event.is_high_impact:
                continue
            if event.country.upper() not in self._USD_COUNTRIES:
                continue

            minutes_until = event.minutes_until(now)

            # Block if event is approaching within the window
            if 0 < minutes_until <= window:
                return event, minutes_until

            # Block if event just started and we're still within the window
            if -window <= minutes_until <= 0:
                return event, minutes_until

        return None, 0.0
