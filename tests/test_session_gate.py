"""Tests for SessionGate.

The gate now reads snapshot.as_of instead of datetime.now(), so we inject
the desired ET time directly into the snapshot's as_of field.
"""
from __future__ import annotations

from datetime import datetime, time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from drift.config.models import SessionBlock, SessionsSection
from drift.gates.session_gate import SessionGate
from drift.models import MarketSnapshot

_ET = ZoneInfo("America/New_York")


def _make_config(
    enabled: bool = True,
    blocks: list[tuple[str, str]] | None = None,
    skip: int = 10,
) -> SessionsSection:
    if blocks is None:
        blocks = [("09:40", "11:30"), ("13:30", "15:30")]
    return SessionsSection(
        enabled=enabled,
        blocks=[SessionBlock(start=s, end=e) for s, e in blocks],
        skip_first_n_minutes_after_open=skip,
    )


def _make_snapshot(session: str = "open", as_of: datetime | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        as_of=as_of or datetime.now(tz=ZoneInfo("UTC")),
        symbol="MNQ",
        last_price=21_000.0,
        session=session,
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


def _gate_at_et_time(hour: int, minute: int, config: SessionsSection | None = None) -> SessionGate:
    """Return a SessionGate whose internal clock reads HH:MM ET."""
    cfg = config or _make_config()
    gate = SessionGate(cfg)
    gate._now_et = lambda: datetime(2026, 4, 14, hour, minute, 0, tzinfo=_ET)  # noqa: SLF001
    return gate


# Patch the gate to inject a controllable clock rather than mocking datetime.now globally.
# We achieve this by monkey-patching a helper method on the gate instance.
class _ClockableSessionGate(SessionGate):
    """Test subclass that allows injecting the current ET time."""

    def __init__(self, config: SessionsSection, et_hour: int, et_minute: int) -> None:
        super().__init__(config)
        self._fixed_et = datetime(2026, 4, 14, et_hour, et_minute, 0, tzinfo=_ET)

    def evaluate(self, snapshot: MarketSnapshot):
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        import drift.gates.session_gate as _mod

        with patch.object(_mod, "datetime") as mock_dt:
            mock_dt.now.return_value = self._fixed_et
            mock_dt.combine = datetime.combine
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            return super().evaluate(snapshot)


# ------------------------------------------------------------------ #
# Since patching datetime is complex, we test via the real time gate  #
# but inject ET time through a thin wrapper around the module-level   #
# datetime.now call inside session_gate.                              #
# ------------------------------------------------------------------ #


def _evaluate_at(et_hour: int, et_minute: int, config: SessionsSection, session: str = "open"):
    """Evaluate SessionGate with snapshot.as_of set to the given ET time."""
    as_of = datetime(2026, 4, 14, et_hour, et_minute, 0, tzinfo=_ET)
    snapshot = _make_snapshot(session=session, as_of=as_of)
    gate = SessionGate(config)
    return gate.evaluate(snapshot)


class TestSessionGateDisabled:
    def test_disabled_always_passes_regardless_of_time(self):
        cfg = _make_config(enabled=False)
        result = _evaluate_at(3, 0, cfg)
        assert result.passed
        assert "disabled" in result.reason.lower()

    def test_disabled_passes_even_on_closed_session(self):
        cfg = _make_config(enabled=False)
        result = _evaluate_at(3, 0, cfg, session="closed")
        assert result.passed


class TestSessionGateClosedMarket:
    def test_blocks_when_session_not_open(self):
        result = _evaluate_at(10, 0, _make_config(), session="pre")
        assert not result.passed
        assert "pre" in result.reason

    def test_blocks_when_session_closed(self):
        result = _evaluate_at(10, 0, _make_config(), session="closed")
        assert not result.passed

    def test_blocks_when_session_post(self):
        result = _evaluate_at(10, 0, _make_config(), session="post")
        assert not result.passed


class TestSessionGateSkipWindow:
    def test_blocks_at_exact_market_open(self):
        """09:30 is within the skip window (skip=10 → skip until 09:40)."""
        result = _evaluate_at(9, 30, _make_config(skip=10))
        assert not result.passed
        assert "skip" in result.reason.lower()

    def test_blocks_just_before_skip_end(self):
        result = _evaluate_at(9, 39, _make_config(skip=10))
        assert not result.passed

    def test_passes_at_skip_end(self):
        """09:40 is exactly when skip ends AND first block starts."""
        result = _evaluate_at(9, 40, _make_config(skip=10))
        assert result.passed

    def test_no_skip_when_zero(self):
        """skip=0 disables the skip window entirely."""
        cfg = _make_config(skip=0, blocks=[("09:30", "11:30")])
        result = _evaluate_at(9, 30, cfg)
        assert result.passed


class TestSessionGateTradingBlocks:
    def test_passes_inside_first_block(self):
        result = _evaluate_at(10, 30, _make_config())
        assert result.passed
        assert "09:40" in result.reason

    def test_passes_inside_second_block(self):
        result = _evaluate_at(14, 0, _make_config())
        assert result.passed
        assert "13:30" in result.reason

    def test_passes_at_block_start_boundary(self):
        result = _evaluate_at(9, 40, _make_config())
        assert result.passed

    def test_passes_at_block_end_boundary(self):
        result = _evaluate_at(11, 30, _make_config())
        assert result.passed

    def test_blocks_between_blocks(self):
        """12:00 is between 11:30 and 13:30 — the lunch gap."""
        result = _evaluate_at(12, 0, _make_config())
        assert not result.passed
        assert "12:00" in result.reason

    def test_blocks_before_first_block(self):
        result = _evaluate_at(9, 15, _make_config(skip=0))
        assert not result.passed

    def test_blocks_after_last_block(self):
        result = _evaluate_at(16, 0, _make_config())
        assert not result.passed
