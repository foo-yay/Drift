from __future__ import annotations

import csv
import os
from pathlib import Path

from drift.replay.engine import ReplaySummary


_COLUMNS = [
    "timestamp",
    "symbol",
    "bias",
    "setup_type",
    "confidence",
    "entry_min",
    "entry_max",
    "stop_loss",
    "take_profit_1",
    "take_profit_2",
    "reward_risk_ratio",
    "outcome",
    "pnl_points",
    "minutes_to_exit",
    "exit_price",
]


def export_replay_csv(summary: ReplaySummary, path: str) -> None:
    """Write all trade-plan events from a replay run to a CSV file.

    Only rows where a trade plan was issued (TRADE_PLAN_ISSUED) are included.
    Each row includes the outcome fields if they were resolved.
    """
    os.makedirs(Path(path).parent, exist_ok=True)

    rows = []
    for event in summary.events:
        if event.final_outcome != "TRADE_PLAN_ISSUED":
            continue

        tp = event.trade_plan or {}
        outcome = event.replay_outcome or {}

        rows.append({
            "timestamp": event.event_time.strftime("%Y-%m-%d %H:%M"),
            "symbol": event.symbol,
            "bias": tp.get("bias", ""),
            "setup_type": tp.get("setup_type", ""),
            "confidence": tp.get("confidence", ""),
            "entry_min": tp.get("entry_min", ""),
            "entry_max": tp.get("entry_max", ""),
            "stop_loss": tp.get("stop_loss", ""),
            "take_profit_1": tp.get("take_profit_1", ""),
            "take_profit_2": tp.get("take_profit_2", ""),
            "reward_risk_ratio": tp.get("reward_risk_ratio", ""),
            "outcome": outcome.get("outcome", ""),
            "pnl_points": outcome.get("pnl_points", ""),
            "minutes_to_exit": outcome.get("minutes_elapsed", ""),
            "exit_price": outcome.get("exit_price", ""),
        })

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
