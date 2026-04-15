"""Macro economic events panel — today's ForexFactory events with countdown."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from drift.gates.calendar_provider import ForexFactoryCalendarProvider
from drift.models import CalendarEvent

_ET = ZoneInfo("America/New_York")

_IMPACT_ICON = {
    "High":    "🔴",
    "Medium":  "🟡",
    "Low":     "⚪",
    "Holiday": "🏖️",
}

_provider: ForexFactoryCalendarProvider | None = None


def _get_provider() -> ForexFactoryCalendarProvider:
    global _provider
    if _provider is None:
        _provider = ForexFactoryCalendarProvider(cache_ttl_minutes=60)
    return _provider


def _countdown(event_utc: datetime) -> str:
    now = datetime.now(tz=timezone.utc)
    delta = event_utc - now
    total_sec = int(delta.total_seconds())
    if total_sec < 0:
        mins_ago = abs(total_sec) // 60
        return f"{mins_ago}m ago"
    if total_sec < 3600:
        return f"in {total_sec // 60}m"
    hours = total_sec // 3600
    mins  = (total_sec % 3600) // 60
    return f"in {hours}h {mins}m"


def render_news_panel(filter_countries: list[str] | None = None) -> None:
    """Render macro economic events for today.

    Shows only today's events, filtered to *filter_countries* if provided
    (defaults to USD / US events).  Events are sorted chronologically.
    """
    countries = set(filter_countries or ["USD", "US"])
    st.markdown("**Today's Macro Events**")

    try:
        events = _get_provider().get_events()
    except Exception:  # noqa: BLE001
        st.caption("⚠️ Could not load economic calendar.")
        return

    now_et = datetime.now(tz=_ET)
    today_et = now_et.date()

    today_events = [
        e for e in events
        if e.event_time is not None
        and e.event_time.astimezone(_ET).date() == today_et
        and (not countries or e.country in countries)
        and e.impact in ("High", "Medium")
    ]

    if not today_events:
        st.caption("No high/medium impact events today.")
        return

    today_events.sort(key=lambda e: e.event_time)

    for e in today_events:
        icon = _IMPACT_ICON.get(e.impact, "⚪")
        et_str = e.event_time.astimezone(_ET).strftime("%H:%M")
        cd = _countdown(e.event_time)
        st.markdown(f"{icon} **{et_str} ET** — {e.title} *({cd})*")
