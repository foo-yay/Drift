"""Tests for NewsGate — pass/fail cases per gate contract.

Gate rules:
    - If gate is disabled (news_gate_enabled=False) → always passes
    - HIGH impact USD event within news_blackout_minutes (approaching) → blocks
    - HIGH impact USD event within news_blackout_minutes (just started) → blocks
    - HIGH impact USD event outside window → passes
    - Non-USD HIGH impact event → passes (USD-only filter)
    - Medium/Low impact USD events → passes
    - Provider returns empty list (network failure) → passes (safe degradation)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from drift.config.models import GatesSection
from drift.gates.news_gate import NewsGate
from drift.models import CalendarEvent, MarketSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> GatesSection:
    defaults = {
        "regime_enabled": True,
        "min_trend_score": 35,
        "min_momentum_score": 30,
        "block_on_extreme_volatility": True,
        "cooldown_enabled": True,
        "kill_switch_enabled": True,
        "kill_switch_path": "data/.kill_switch",
        "news_gate_enabled": True,
        "news_blackout_minutes": 30,
    }
    defaults.update(overrides)
    return GatesSection(**defaults)


def _make_event(
    *,
    minutes_from_now: float,
    country: str = "USD",
    impact: str = "High",
    title: str = "NFP",
) -> CalendarEvent:
    now = datetime.now(tz=timezone.utc)
    return CalendarEvent(
        title=title,
        country=country,
        event_time=now + timedelta(minutes=minutes_from_now),
        impact=impact,  # type: ignore[arg-type]
    )


def _make_snapshot() -> MarketSnapshot:
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


def _gate_with_events(events: list[CalendarEvent], config: GatesSection | None = None) -> NewsGate:
    cfg = config or _make_config()
    gate = NewsGate(cfg)
    gate._provider.get_events = MagicMock(return_value=events)  # noqa: SLF001
    return gate


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNewsGateDisabled:
    def test_disabled_always_passes(self):
        event = _make_event(minutes_from_now=5)  # would normally block
        gate = _gate_with_events([event], config=_make_config(news_gate_enabled=False))
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "disabled" in result.reason.lower()

    def test_disabled_passes_with_no_events(self):
        gate = _gate_with_events([], config=_make_config(news_gate_enabled=False))
        result = gate.evaluate(_make_snapshot())
        assert result.passed


class TestNewsGateBlocking:
    def test_blocks_approaching_event_within_window(self):
        """HIGH impact USD NFP 15 min away → within 30-min window → BLOCK."""
        event = _make_event(minutes_from_now=15)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
        assert "NFP" in result.reason

    def test_blocks_at_window_boundary(self):
        """Just inside the pre-event boundary (29.9 min) → still blocks."""
        event = _make_event(minutes_from_now=29.9)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed

    def test_blocks_event_just_started(self):
        """Event started 10 min ago → within 30-min post window → BLOCK."""
        event = _make_event(minutes_from_now=-10)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed

    def test_blocks_at_post_window_boundary(self):
        """Just inside 30 min post-event window → blocks."""
        event = _make_event(minutes_from_now=-29.9)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert not result.passed

    def test_reason_contains_event_title(self):
        event = _make_event(minutes_from_now=5, title="CPI m/m")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert "CPI m/m" in result.reason
        assert "USD" in result.reason

    def test_reason_contains_window_size(self):
        gate = _gate_with_events([_make_event(minutes_from_now=10)], config=_make_config(news_blackout_minutes=45))
        result = gate.evaluate(_make_snapshot())
        assert "45" in result.reason


class TestNewsGatePassing:
    def test_passes_when_event_beyond_window(self):
        """Event 60 min away → outside 30-min window → passes."""
        event = _make_event(minutes_from_now=60)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_when_event_long_past(self):
        """Event 60 min ago → past 30-min post window → passes."""
        event = _make_event(minutes_from_now=-60)
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_for_non_usd_event(self):
        """EUR HIGH impact event → filtered out → passes."""
        event = _make_event(minutes_from_now=10, country="EUR")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_for_medium_impact_usd(self):
        event = _make_event(minutes_from_now=5, impact="Medium")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_for_low_impact_usd(self):
        event = _make_event(minutes_from_now=5, impact="Low")
        gate = _gate_with_events([event])
        result = gate.evaluate(_make_snapshot())
        assert result.passed

    def test_passes_with_empty_event_list(self):
        """Safe degradation: provider returns empty list."""
        gate = _gate_with_events([])
        result = gate.evaluate(_make_snapshot())
        assert result.passed
        assert "no high-impact" in result.reason.lower()


class TestNewsGateName:
    def test_gate_name(self):
        gate = NewsGate(_make_config())
        assert gate.name == "news"


class TestNewsGateCustomWindow:
    def test_custom_zero_minute_window_never_blocks(self):
        """news_blackout_minutes=0 → effectively disabled (0-width window)."""
        event = _make_event(minutes_from_now=0)  # exactly at event time
        gate = _gate_with_events([event], config=_make_config(news_blackout_minutes=0))
        result = gate.evaluate(_make_snapshot())
        # minutes_until == 0 is exactly at start; both conditions use strict inequality
        # on the pre-event side, so this is "just started" with a 0-width after window.
        # The gate should pass since range is [-0, 0] which is a point, not a window.
        assert result.passed

    def test_custom_60_minute_window_blocks_far_event(self):
        """With 60-min window, an event 55 min away should block."""
        event = _make_event(minutes_from_now=55)
        gate = _gate_with_events([event], config=_make_config(news_blackout_minutes=60))
        result = gate.evaluate(_make_snapshot())
        assert not result.passed
