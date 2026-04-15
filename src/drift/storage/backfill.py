"""Outcome backfill — resolves live TRADE_PLAN_ISSUED signals retroactively.

For each unresolved live signal in the JSONL log (``replay_outcome`` is null)
this module fetches 1m bars from yfinance starting at the signal time and runs
``resolve_outcome`` to determine whether the trade hit TP1/TP2 or was stopped.

The JSONL file is rewritten atomically: a temp file is built line-by-line and
then moved over the original only if no errors occurred.

Usage::

    from drift.storage.backfill import backfill_outcomes

    resolved, skipped = backfill_outcomes(
        log_path="logs/events.jsonl",
        symbol="MNQ=F",
        max_hold_minutes=90,  # fallback when plan has no hold limit
    )
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from drift.models import TradePlan
from drift.replay.outcome import resolve_outcome


def backfill_outcomes(
    log_path: str | Path,
    symbol: str,
    max_hold_minutes: int = 90,
) -> tuple[int, int]:
    """Resolve all unresolved live TRADE_PLAN_ISSUED events in *log_path*.

    Only events where:
    - ``final_outcome == "TRADE_PLAN_ISSUED"``
    - ``replay_outcome`` is ``null`` / missing
    - ``source`` is ``"live"`` (or missing — legacy events default to ``"live"``)

    are candidates.  Replay events are skipped because they already have
    outcomes resolved during the replay run itself.

    Args:
        log_path:         Path to the JSONL event log.
        symbol:           Ticker symbol — used for the yfinance fetch.
        max_hold_minutes: Fallback max hold time when the stored plan omits
                          ``max_hold_minutes``.  Defaults to 90.

    Returns:
        ``(resolved, skipped)`` counts.

    Raises:
        FileNotFoundError: If *log_path* does not exist.
        RuntimeError:      If the atomic rewrite fails.
    """
    log_path = Path(log_path)
    if not log_path.exists():
        raise FileNotFoundError(log_path)

    # Lazy import so the rest of the codebase doesn't pay the yfinance cost.
    import yfinance as yf  # noqa: PLC0415

    resolved = 0
    skipped = 0

    # Read all lines first so we can determine the date range we need.
    raw_lines: list[str] = log_path.read_text(encoding="utf-8").splitlines()

    # Identify which lines need patching and collect the bar windows we need.
    candidates: list[tuple[int, dict[str, Any]]] = []  # (line_idx, parsed_dict)
    for idx, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("final_outcome") != "TRADE_PLAN_ISSUED":
            continue
        if obj.get("replay_outcome") is not None:
            continue
        source = obj.get("source", "live")
        if source != "live":
            continue
        if not obj.get("trade_plan"):
            continue

        candidates.append((idx, obj))

    if not candidates:
        return 0, 0

    # Fetch bars in one pass per unique date to reduce API calls.
    # Group candidates by date (UTC) so we can build one yfinance call per date.
    from collections import defaultdict  # noqa: PLC0415

    date_groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for idx, obj in candidates:
        event_time_str = obj.get("event_time", "")
        try:
            event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            skipped += 1
            continue
        date_key = event_time.strftime("%Y-%m-%d")
        date_groups[date_key].append((idx, obj, event_time))  # type: ignore[arg-type]

    # Map line_idx → patched replay_outcome dict
    patches: dict[int, dict[str, Any]] = {}

    ticker_sym = _resolve_ticker(symbol)

    for date_key, group in date_groups.items():
        # Fetch 1m bars for the full day (yfinance end is exclusive, add 1 day)
        from datetime import date as date_type  # noqa: PLC0415
        start_dt = datetime.strptime(date_key, "%Y-%m-%d").date()
        end_dt = start_dt + timedelta(days=2)  # generous window covers overnight holds

        try:
            ticker = yf.Ticker(ticker_sym)
            df = ticker.history(
                interval="1m",
                start=start_dt.isoformat(),
                end=end_dt.isoformat(),
                auto_adjust=True,
            )
        except Exception:  # noqa: BLE001
            skipped += len(group)
            continue

        if df.empty:
            skipped += len(group)
            continue

        # Normalise index to UTC-aware datetimes
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        from drift.models import Bar  # noqa: PLC0415

        all_bars = [
            Bar(
                timestamp=row.Index.to_pydatetime(),
                open=float(row.Open),
                high=float(row.High),
                low=float(row.Low),
                close=float(row.Close),
                volume=float(row.Volume),
                timeframe="1m",
                symbol=symbol,
            )
            for row in df.itertuples()
        ]

        for entry in group:
            idx, obj, event_time = entry  # type: ignore[misc]

            # Bars strictly after the signal bar
            bars_after = [b for b in all_bars if b.timestamp > event_time]

            try:
                plan_dict = obj["trade_plan"]
                # Ensure max_hold_minutes has a sane fallback
                if not plan_dict.get("max_hold_minutes"):
                    plan_dict = {**plan_dict, "max_hold_minutes": max_hold_minutes}
                plan = TradePlan.model_validate(plan_dict)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue

            if not bars_after:
                skipped += 1
                continue

            try:
                outcome = resolve_outcome(plan, bars_after)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue

            patches[idx] = {
                "outcome": outcome.outcome,
                "bars_elapsed": outcome.bars_elapsed,
                "minutes_elapsed": outcome.minutes_elapsed,
                "exit_price": outcome.exit_price,
                "pnl_points": round(outcome.pnl_points, 2),
            }
            resolved += 1

    if not patches:
        return 0, skipped

    # Atomic rewrite — build a temp file alongside the log, then rename.
    log_dir = log_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=log_dir, prefix=".backfill_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for idx, line in enumerate(raw_lines):
                line_stripped = line.strip()
                if not line_stripped:
                    fh.write(line + "\n" if not line.endswith("\n") else line)
                    continue

                if idx in patches:
                    try:
                        obj = json.loads(line_stripped)
                        obj["replay_outcome"] = patches[idx]
                        fh.write(json.dumps(obj) + "\n")
                        continue
                    except json.JSONDecodeError:
                        pass  # fall through to write unmodified

                fh.write(line_stripped + "\n")

        os.replace(tmp_path, log_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return resolved, skipped


def _resolve_ticker(symbol: str) -> str:
    """Map Drift instrument symbols to yfinance tickers."""
    _MAP = {
        "MNQ=F": "MNQ=F",
        "ES=F": "ES=F",
        "NQ=F": "NQ=F",
    }
    return _MAP.get(symbol, symbol)
