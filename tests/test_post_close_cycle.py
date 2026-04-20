"""Tests for post-close immediate LLM cycle."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _poll_positions — fires post-close cycle on SL/TP hits
# ---------------------------------------------------------------------------

class TestPollPositionsPostClose:
    """Verify _poll_positions fires _fire_post_close_cycle on close events."""

    def _make_scheduler(self):
        """Build a minimal BackgroundScheduler-like mock."""
        from drift.gui.scheduler import _SchedulerState

        sched = MagicMock()
        sched.state = _SchedulerState()
        sched._config_path = "config/settings.yaml"
        sched._last_watch_cycle_utc = None
        sched._last_watch_was_trade_plan = False
        sched._cooldown_timer_lock = threading.Lock()
        sched._cooldown_timer = None
        sched._fill_timeout_assessed = set()
        sched._pending_post_close_events = []
        sched._pending_post_close_lock = threading.Lock()
        return sched

    def test_fires_on_sl_hit(self):
        """When poll_positions returns SL_HIT, post-close cycle fires."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        sched.state.running = False

        config = MagicMock()
        config.broker.enabled = True
        config.storage.sqlite_path = "data/test.db"

        mock_mgr = MagicMock()
        mock_mgr.poll_positions.return_value = [
            {"type": "SL_HIT", "position_id": 5, "exit_price": 20900.0},
        ]

        with patch("drift.brokers.position_manager.PositionManager", return_value=mock_mgr), \
             patch("drift.gui.state._PROJECT_ROOT", MagicMock(__truediv__=lambda s, o: o)):
            BackgroundScheduler._poll_positions(sched, config)

        sched._queue_post_close_cycle.assert_called_once()
        args = sched._queue_post_close_cycle.call_args[0][0]
        assert len(args) == 1
        assert args[0]["type"] == "SL_HIT"

    def test_no_fire_on_entry_fill(self):
        """Entry fills should NOT trigger post-close cycle."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        sched.state.running = False

        config = MagicMock()
        config.broker.enabled = True
        config.storage.sqlite_path = "data/test.db"

        mock_mgr = MagicMock()
        mock_mgr.poll_positions.return_value = [
            {"type": "ENTRY_FILLED", "position_id": 5, "fill_price": 21000.0},
        ]

        with patch("drift.brokers.position_manager.PositionManager", return_value=mock_mgr), \
             patch("drift.gui.state._PROJECT_ROOT", MagicMock(__truediv__=lambda s, o: o)):
            BackgroundScheduler._poll_positions(sched, config)

        sched._fire_post_close_cycle.assert_not_called()

    def test_queues_when_already_running(self):
        """If a cycle is running, post-close cycle is queued (not dropped)."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        sched.state.running = True  # cycle in progress

        config = MagicMock()
        config.broker.enabled = True
        config.storage.sqlite_path = "data/test.db"

        mock_mgr = MagicMock()
        mock_mgr.poll_positions.return_value = [
            {"type": "TP1_HIT", "position_id": 5, "exit_price": 21040.0},
        ]

        with patch("drift.brokers.position_manager.PositionManager", return_value=mock_mgr), \
             patch("drift.gui.state._PROJECT_ROOT", MagicMock(__truediv__=lambda s, o: o)):
            BackgroundScheduler._poll_positions(sched, config)

        # _queue_post_close_cycle is still called — it decides internally to defer
        sched._queue_post_close_cycle.assert_called_once()

    def test_no_fire_when_broker_disabled(self):
        """When broker is disabled, no polling occurs."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        config = MagicMock()
        config.broker.enabled = False

        BackgroundScheduler._poll_positions(sched, config)
        sched._queue_post_close_cycle.assert_not_called()


# ---------------------------------------------------------------------------
# _queue_post_close_cycle — fires immediately or defers
# ---------------------------------------------------------------------------

class TestQueuePostCloseCycle:
    """Verify _queue_post_close_cycle fires immediately or defers correctly."""

    def _make_scheduler(self):
        from drift.gui.scheduler import _SchedulerState
        sched = MagicMock()
        sched.state = _SchedulerState()
        sched._pending_post_close_events = []
        sched._pending_post_close_lock = threading.Lock()
        return sched

    def test_fires_immediately_when_not_running(self):
        """When no cycle is running, fires immediately via _fire_post_close_cycle."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        sched.state.running = False
        events = [{"type": "SL_HIT", "position_id": 1}]

        BackgroundScheduler._queue_post_close_cycle(sched, events)

        sched._fire_post_close_cycle.assert_called_once_with(events)
        assert sched._pending_post_close_events == []

    def test_defers_when_running(self):
        """When a cycle is running, events are queued and _fire is not called."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        sched.state.running = True
        events = [{"type": "TP1_HIT", "position_id": 2}]

        BackgroundScheduler._queue_post_close_cycle(sched, events)

        sched._fire_post_close_cycle.assert_not_called()
        assert sched._pending_post_close_events == events


# ---------------------------------------------------------------------------
# _drain_pending_post_close — fires deferred events after cycle completes
# ---------------------------------------------------------------------------

class TestDrainPendingPostClose:
    """Verify _drain_pending_post_close flushes queued events."""

    def _make_scheduler(self):
        sched = MagicMock()
        sched._pending_post_close_events = []
        sched._pending_post_close_lock = threading.Lock()
        return sched

    def test_fires_queued_events(self):
        """Drains the queue and calls _fire_post_close_cycle with all events."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        sched._pending_post_close_events = [
            {"type": "SL_HIT", "position_id": 1},
            {"type": "HOLD_EXPIRY", "position_id": 0},
        ]

        BackgroundScheduler._drain_pending_post_close(sched)

        sched._fire_post_close_cycle.assert_called_once()
        fired = sched._fire_post_close_cycle.call_args[0][0]
        assert len(fired) == 2
        assert sched._pending_post_close_events == []

    def test_no_fire_when_empty(self):
        """No call to _fire_post_close_cycle when the queue is empty."""
        from drift.gui.scheduler import BackgroundScheduler

        sched = self._make_scheduler()
        BackgroundScheduler._drain_pending_post_close(sched)
        sched._fire_post_close_cycle.assert_not_called()


# ---------------------------------------------------------------------------
# _fire_post_close_cycle — clears cooldown and runs cycle
# ---------------------------------------------------------------------------

class TestFirePostCloseCycle:
    """Verify _fire_post_close_cycle clears timers and runs a cycle."""

    def test_clears_cooldown_and_runs_cycle(self):
        from drift.gui.scheduler import BackgroundScheduler

        sched = MagicMock()
        sched._last_watch_cycle_utc = "some_timestamp"

        BackgroundScheduler._fire_post_close_cycle(sched, [{"type": "SL_HIT", "position_id": 5}])

        sched._cancel_cooldown_timer.assert_called_once()
        assert sched._last_watch_cycle_utc is None
        sched._run_cycle.assert_called_once_with(trigger="post_close")

    def test_trigger_label_is_post_close(self):
        from drift.gui.scheduler import BackgroundScheduler

        sched = MagicMock()
        BackgroundScheduler._fire_post_close_cycle(sched, [{"type": "TP2_HIT", "position_id": 7}])
        sched._run_cycle.assert_called_once_with(trigger="post_close")
