from __future__ import annotations

import json
import ssl
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import certifi

from drift.models import CalendarEvent

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_DISK_CACHE_PATH = Path(tempfile.gettempdir()) / "drift_calendar_cache.json"


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


def _parse_raw_events(raw: list[dict]) -> list[CalendarEvent]:
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
    return events


class ForexFactoryCalendarProvider:
    """Fetches the current-week economic calendar from ForexFactory's public JSON feed.

    Caching strategy (two layers):
      1. In-process: avoids repeated fetches within a long-running process.
      2. Disk (``/tmp/drift_calendar_cache.json``): persists across process
         restarts so rapid manual ``drift run`` invocations don't re-hit the
         endpoint every time and trigger HTTP 429 rate-limiting.

    On a network failure the provider first tries to serve the last known good
    disk cache (even if stale), then falls back to an empty list so the gate
    degrades gracefully rather than crashing.
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh(self, now: datetime) -> None:
        # 1. Try disk cache first — avoids hitting the network if a recent
        #    fetch was made by a previous process invocation.
        if self._load_disk_cache(now):
            return

        # 2. Fetch from network.
        try:
            req = urllib.request.Request(
                _FEED_URL,
                headers={"User-Agent": "Drift/1.0 (economic calendar gate; contact via GitHub)"},
            )
            with urllib.request.urlopen(req, timeout=8, context=_SSL_CONTEXT) as resp:
                raw: list[dict] = json.loads(resp.read().decode("utf-8"))

            events = _parse_raw_events(raw)
            self._cached_events = events
            self._last_fetch = now
            self._write_disk_cache(raw, now)

        except Exception as exc:  # noqa: BLE001
            # Serve stale disk cache (any age) rather than returning empty —
            # a day-old calendar is still better than no calendar.
            if self._load_disk_cache(now, ignore_ttl=True):
                print(f"[calendar] WARNING: network error ({exc}); serving stale disk cache")
            else:
                print(f"[calendar] WARNING: could not refresh calendar feed: {exc}")
            self._last_fetch = now  # back off; don't hammer the endpoint on failure

    def _load_disk_cache(self, now: datetime, *, ignore_ttl: bool = False) -> bool:
        """Try to load events from the disk cache. Returns True on success."""
        if not _DISK_CACHE_PATH.exists():
            return False
        try:
            payload = json.loads(_DISK_CACHE_PATH.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            age_seconds = (now - fetched_at).total_seconds()
            if not ignore_ttl and age_seconds > self._cache_ttl_seconds:
                return False
            self._cached_events = _parse_raw_events(payload["events"])
            self._last_fetch = now
            return True
        except Exception:  # noqa: BLE001
            return False

    def _write_disk_cache(self, raw: list[dict], now: datetime) -> None:
        """Persist the raw event list to disk for use by future process invocations."""
        try:
            payload = {"fetched_at": now.isoformat(), "events": raw}
            _DISK_CACHE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass  # disk write failure is non-fatal

