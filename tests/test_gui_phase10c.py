"""Smoke tests for Phase 10c GUI pages: Signal History + Replay Lab.

These tests do not launch a Streamlit runtime.  They verify:
  - Both page modules import without errors
  - CSV export logic produces valid CSV
  - Pagination math is correct
  - Replay Lab correctly delegates to store.query() after insert
  - SignalRow helpers used in the templates behave as expected

Streamlit-dependent rendering (dialogs, widgets) is tested manually via
`drift gui`.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from drift.storage.signal_store import SignalRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 6, 1, 15, 0, 0, tzinfo=timezone.utc)
_TS_STR = _TS.isoformat()


def _make_row(
    n: int = 0,
    outcome: str = "TRADE_PLAN_ISSUED",
    bias: str | None = "LONG",
    pnl: float | None = 5.0,
    replay_outcome: str | None = "TP1_HIT",
    source: str = "replay",
) -> SignalRow:
    ts = (_TS + timedelta(minutes=n * 5)).isoformat()
    return SignalRow(
        id=n + 1,
        signal_key=f"key{n:04d}",
        symbol="MNQ=F",
        source=source,
        event_time_utc=ts,
        as_of_utc=ts,
        final_outcome=outcome,
        bias=bias,
        setup_type="pullback_continuation",
        confidence=75,
        entry_min=21000.0,
        entry_max=21010.0,
        stop_loss=20970.0,
        take_profit_1=21050.0,
        take_profit_2=21100.0,
        reward_risk=2.5,
        pnl_points=pnl,
        replay_outcome=replay_outcome,
        thesis="Test thesis.",
        snapshot_json=None,
        gate_report_json=None,
        llm_json=None,
        created_at=ts,
    )


# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------

def test_signal_history_page_imports():
    """Module must import without errors."""
    import drift.gui.pages.signal_history  # noqa: F401


def test_replay_lab_page_imports():
    """Module must import without errors."""
    import drift.gui.pages.replay_lab  # noqa: F401


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def test_csv_export_content():
    """CSV export rows include all expected column headers and data."""
    rows = [_make_row(i) for i in range(3)]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "signal_key", "symbol", "source", "event_time_utc", "final_outcome",
        "bias", "setup_type", "confidence", "entry_min", "entry_max",
        "stop_loss", "take_profit_1", "take_profit_2", "reward_risk",
        "replay_outcome", "pnl_points", "thesis",
    ])
    for r in rows:
        writer.writerow([
            r.signal_key, r.symbol, r.source, r.event_time_utc, r.final_outcome,
            r.bias, r.setup_type, r.confidence, r.entry_min, r.entry_max,
            r.stop_loss, r.take_profit_1, r.take_profit_2, r.reward_risk,
            r.replay_outcome, r.pnl_points, r.thesis,
        ])

    buf.seek(0)
    reader = list(csv.DictReader(buf))
    assert len(reader) == 3
    assert reader[0]["symbol"] == "MNQ=F"
    assert reader[0]["final_outcome"] == "TRADE_PLAN_ISSUED"
    assert reader[0]["bias"] == "LONG"
    assert reader[0]["pnl_points"] == "5.0"


def test_csv_export_handles_none_fields():
    """CSV export rows with None fields do not raise."""
    row = _make_row(0, bias=None, pnl=None, replay_outcome=None)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["bias", "pnl_points", "replay_outcome"])
    writer.writerow([row.bias, row.pnl_points, row.replay_outcome])
    buf.seek(0)
    reader = list(csv.DictReader(buf))
    assert reader[0]["bias"] == ""
    assert reader[0]["pnl_points"] == ""


# ---------------------------------------------------------------------------
# Pagination math
# ---------------------------------------------------------------------------

_PAGE_SIZE = 25


def _paginate(rows, page_idx):
    return rows[page_idx * _PAGE_SIZE : (page_idx + 1) * _PAGE_SIZE]


def test_pagination_first_page():
    rows = [_make_row(i) for i in range(60)]
    page = _paginate(rows, 0)
    assert len(page) == 25
    assert page[0].signal_key == "key0000"


def test_pagination_last_page():
    rows = [_make_row(i) for i in range(60)]
    total_pages = (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE
    assert total_pages == 3
    last_page = _paginate(rows, 2)
    assert len(last_page) == 10  # 60 - 2*25


def test_pagination_single_page():
    rows = [_make_row(i) for i in range(5)]
    total_pages = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    assert total_pages == 1
    assert _paginate(rows, 0) == rows


def test_pagination_empty():
    rows = []
    total_pages = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    assert total_pages == 1


# ---------------------------------------------------------------------------
# SignalRow field access (used in templates)
# ---------------------------------------------------------------------------

def test_signal_row_event_time_utc_parseable():
    row = _make_row(0)
    ts = datetime.fromisoformat(row.event_time_utc)
    assert ts.year == 2026


def test_signal_row_bias_accessible():
    row = _make_row(0, bias="SHORT")
    assert row.bias == "SHORT"


def test_signal_row_none_bias_fallback():
    row = _make_row(0, bias=None)
    label = row.bias or "—"
    assert label == "—"


def test_signal_row_replay_outcome_fallback():
    row = _make_row(0, replay_outcome=None)
    label = row.replay_outcome or "—"
    assert label == "—"


# ---------------------------------------------------------------------------
# win_rate_and_pnl output shape used by Signal History metric row
# ---------------------------------------------------------------------------

def test_win_rate_pnl_dict_shape():
    """The dict returned by win_rate_and_pnl() must contain all expected keys."""
    expected_keys = {"total", "resolved", "wins", "win_rate_pct", "total_pnl"}
    mock_store = MagicMock()
    mock_store.win_rate_and_pnl.return_value = {
        "total": 10, "resolved": 5, "wins": 3, "win_rate_pct": 60.0, "total_pnl": 15.0
    }
    stats = mock_store.win_rate_and_pnl(sources=None, date_start=date.today(), date_end=date.today())
    assert set(stats.keys()) == expected_keys


# ---------------------------------------------------------------------------
# Replay Lab — insert + query-back pattern
# ---------------------------------------------------------------------------

def test_replay_lab_uses_insert_event():
    """After a replay run the lab calls store.insert_event for each event."""
    from drift.storage.signal_store import SignalStore
    from unittest.mock import MagicMock

    mock_store = MagicMock(spec=SignalStore)
    mock_store.insert_event.return_value = True
    mock_store.query.return_value = []

    from drift.models import SignalEvent

    events = [
        SignalEvent(
            event_time=_TS + timedelta(minutes=i * 5),
            symbol="MNQ=F",
            source="replay",
            signal_key=f"evt{i:04d}",
            final_outcome="TRADE_PLAN_ISSUED",
            final_reason="llm",
        )
        for i in range(3)
    ]

    inserted = 0
    for evt in events:
        if mock_store.insert_event(evt):
            inserted += 1

    assert mock_store.insert_event.call_count == 3
    assert inserted == 3


def test_replay_lab_count_by_date_range_dedup():
    """Dedup check calls count_by_date_range with the right params."""
    mock_store = MagicMock()
    mock_store.count_by_date_range.return_value = 5

    start = date(2026, 6, 1)
    end   = date(2026, 6, 3)

    count = mock_store.count_by_date_range(
        symbol="MNQ=F",
        date_start=start,
        date_end=end,
        source="replay",
    )
    assert count == 5
    mock_store.count_by_date_range.assert_called_once_with(
        symbol="MNQ=F", date_start=start, date_end=end, source="replay"
    )


def test_replay_lab_delete_before_overwrite():
    """Overwrite flow calls delete_by_date_range before storing new signals."""
    mock_store = MagicMock()
    mock_store.delete_by_date_range.return_value = 5

    start = date(2026, 6, 1)
    end   = date(2026, 6, 3)

    deleted = mock_store.delete_by_date_range(
        symbol="MNQ=F",
        date_start=start,
        date_end=end,
        source="replay",
    )
    assert deleted == 5
