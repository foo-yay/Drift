from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from drift.models import CalendarEvent

_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_ET = ZoneInfo("America/New_York")

# ForexFactory date/time parsing
# Date: "Apr 10, 2026"   Time: "8:30am" | "All Day" | "Tentative"
_DATE_FMT = "%b %d, %Y"


def _parse_event_time(date_str: str, time_str: str) -> datetime | None:
    """Parse ForexFactory date + time strings into an aware UTC datetime.

    Returns None for all-day or tentative events (no specific time given).
    """
    time_lower = time_str.strip().lower()
    if time_lower in ("all day", "tentative", ""):
        return None

    try:
        date_part = datetime.strptime(date_str.strip(), _DATE_FMT).date()
    except ValueError:
        return None

    # Normalise formats: "8:30am" → "8:30 AM", "12:00pm" → "12:00 PM"
    normalised = time_lower.replace("am", " AM").replace("pm", " PM")
    for fmt in ("%I:%M %p", "%I %p"):
        try:
            time_part = datetime.strptime(normalised.strip(), fmt).time()
            dt_et = datetime.combine(date_part, time_part, tzinfo=_ET)
            return dt_et.astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def _map_impact(raw: str) -> str:
    mapping = {"High": "High", "Medium": "Medium", "Low": "Low", "Holiday": "Holiday"}
    return mapping.get(raw, "Low")


class ForexFactoryCalendarProvider:
    """Fetches the current-week economic calendar from ForexFactory's public JSON feed.

    The feed is cached in-process for `cache_ttl_minutes` to avoid hitting the
    endpoint on every 60-second cycle. A failed fetch logs a warning and returns
    an empty list so the gate degrades gracefully rather than crashing.
    """

    def __init__(self, cache_ttl_minutes: int = 60) -> None:
        self._cache_ttl_seconds = cache_ttl_minutes * 60
        self._cached_events: list[CalendarEvent] = []
        self._last_fetch: datetime | None = None

    def get_events(self) -> list[CalendarEvent]:
        """Return this week's events, refreshing the cache if stale."""
        now = datetime.now(tz=timezone.utc)
        if self._last_fetch is None or (now - self._last_fetch).total_seconds() > self._cache_ttl_seconds:
            self._refresh(now)
        return self._cached_events

    def _refresh(self, now: datetime) -> None:
        try:
            req = urllib.request.Request(
                _FEED_URL,
                headers={"User-Agent": "Drift/1.0 (economic calendar gate; contact via GitHub)"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw: list[dict] = json.loads(resp.read().decode("utf-8"))

            events: list[CalendarEvent] = []
            for item in raw:
                dt = _parse_event_time(item.get("date", ""), item.get("time", ""))
                if dt is None:
                    continue
                events.append(
                    CalendarEvent(
                        title=item.get("title", "Unknown Event"),
                        country=item.get("country", ""),
                        event_time=dt,
                        impact=_map_impact(item.get("impact", "Low")),
                        forecast=item.get("forecast") or None,
                        previous=item.get("previous") or None,
                    )
                )

            self._cached_events = events
            self._last_fetch = now

        except Exception as exc:  # noqa: BLE001
            # Degrade gracefully — an empty calendar means the gate will pass,
            # which is the safer failure mode for a live-trading context.
            print(f"[calendar] WARNING: could not refresh calendar feed: {exc}")
            self._last_fetch = now  # back off; don't hammer the endpoint on failure
