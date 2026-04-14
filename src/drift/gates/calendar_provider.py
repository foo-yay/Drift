from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime, timezone

import certifi

from drift.models import CalendarEvent

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _parse_event_time(date_str: str) -> datetime | None:
    """Parse the ForexFactory ISO 8601 date field into an aware UTC datetime.

    The feed uses full datetimes like ``"2026-04-14T08:30:00-04:00"``.
    Returns None if the string is missing or unparseable.
    """
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).astimezone(timezone.utc)
    except (ValueError, TypeError):
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
            with urllib.request.urlopen(req, timeout=8, context=_SSL_CONTEXT) as resp:
                raw: list[dict] = json.loads(resp.read().decode("utf-8"))

            events: list[CalendarEvent] = []
            for item in raw:
                dt = _parse_event_time(item.get("date", ""))
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
