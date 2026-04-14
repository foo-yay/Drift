"""Tests for CalendarGate — pass/fail cases per gate contract.

Gate rules:
    - If gate is disabled → always passes
    - HIGH impact USD event within buffer_before window → blocks
    - HIGH impact USD event within buffer_after window (just started) → blocks
    - HIGH impact USD event outside both buffers → passes
    - No high-impact events → passes
    - Non-USD high-impact events → passes (filtered by country)
    - Medium/Low impact USD events → passes
    - Provider returns empty list (network failure) → passes (safe degradation)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from drift.config.models import CalendarSection
from drift.gates.calendar_gate import CalendarGate
from drift.models import CalendarEvent, MarketSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ET_UTC_OFFSET = -4  # EDT; the gate only uses UTC internally


def _make_config(**overrides) -> CalendarSection:
    defaults = {
        "enabled": True,
        "buffer_minutes_before": 20,
        "buffer_minutes_after": 10,
        "filter_countries": ["USD"],
        "cache_ttl_minutes": 60,
    }
    defaults.update(overrides)
    return CalendarSection(**defaults)


def _make_event(
    *,
    minutes_from_now: float,
    country: str = "USD",
    impact: str = "High",
    title: str = "CPI m/m",
) -> CalendarEvent:
    now = datetime.now(tz=timezone.utc)
    event_time = now + timedelta(minutes=minutes_from_now)
    return CalendarEvent(
        title=title,
        country=country,
        event_time=event_time,
        impact=impact,  # type: ignore[arg-type]
    )


def _make_snapshot() -> MarketSnapshot:
    """A minimal valid snapshot for gate evaluation."""
    return MarketSnapshot(
        as_of=datetime.now(tz=timezone.utc),
        symbol="MNQ",
        last_price=21_000.0,
        session="regular",
        bars_1m_count=180,
        bars_5m_count=120,
        bars_1h_count=72,
        trend_score=50,
        momentum_score=50,
        volatility_score=50,
        extension_risk=20,
        structure_quality=50,
        pullback_quality=50,
        breakout_quality=50,
        mean_reversion_risk=20,
        session_alignment=50,
        short_trend_state="neutral",
        medium_trend_state="neutral",
        momentum_state="neutral",
        volatility_regime="normal",
    )


def _gate_with_events(events: list[CalendarEvent], config: CalendarSection | None = None) -> CalendarGate:
    """Build a CalendarGate whose provider is mocked to return `events`."""
    cfg = config or _make_config()
    gate = CalendarGate(cfg)
    gate._provider.get_events = MagicMock(return_value=events)  # noqa: SLF001
    return gate


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCalendarGateDisabled:
    def test_disabled_always_passes(self):
        event = _make_event(minutes_from_now=5)  # would normally block
        gate = _gate_with_events([event], config=_make_config(enabled=False))
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "disabled" in result.reason.lower()

    def test_disabled_passes_even_with_no_events(self):
        gate = _gate_with_events([], config=_make_config(enabled=False))
        result = gate.evaluate(_make_snapshot())
        assert result.passed


class TestCalendarGateBlocking:
    def test_blocks_event_approaching_within_buffer(self):
        """HIGH impact event 10 min away → inside 20-min buffer → BLOCK."""
        event = _make_event(minutes_from_now=10)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "CPI m/m" in result.reason

    def test_blocks_event_at_boundary_of_buffer_before(self):
        """Exactly at buffer_before minutes → still blocked."""
        event = _make_event(minutes_from_now=20)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed

    def test_blocks_event_just_started(self):
        """Event started 5 min ago, buffer_after=10 → still blocked."""
        event = _make_event(minutes_from_now=-5)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed

    def test_blocks_event_at_buffer_after_boundary(self):
        """Event started just within buffer_after minutes ago → still blocked."""
        event = _make_event(minutes_from_now=-9.9)  # clearly inside -10-min after-buffer
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed


class TestCalendarGatePassing:
    def test_passes_when_no_events(self):
        gate = _gate_with_events([])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_event_far_in_future(self):
        """Event 60 min away, outside 20-min buffer → passes."""
        event = _make_event(minutes_from_now=60)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_event_long_past(self):
        """Event ended 30 min ago, buffer_after=10 → passes."""
        event = _make_event(minutes_from_now=-30)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_medium_impact_event(self):
        """MEDIUM impact events are not filtered → should pass."""
        event = _make_event(minutes_from_now=5, impact="Medium")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_low_impact_event(self):
        event = _make_event(minutes_from_now=2, impact="Low")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_non_usd_high_impact_event(self):
        """EUR high-impact event should not block a USD gate config."""
        event = _make_event(minutes_from_now=5, country="EUR")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_when_provider_returns_empty_on_network_failure(self):
        """Empty list (network failure graceful degradation) → gate passes."""
        gate = _gate_with_events([])
        result = gate.evaluate(_make_snapshot())
        assert result.passed


class TestCalendarGateReasonString:
    def test_reason_contains_event_title(self):
        event = _make_event(minutes_from_now=5, title="FOMC Statement")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert "FOMC Statement" in result.reason

    def test_reason_contains_country(self):
        event = _make_event(minutes_from_now=5)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert "USD" in result.reason

    def test_pass_reason_mentions_no_events(self):
        gate = _gate_with_events([])
        result = gate.evaluate(_make_snapshot())
        assert "no high-impact" in result.reason.lower()
