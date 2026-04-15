"""Performance context builder for the performance-aware LLM prompt (Phase 9b).

Reads resolved ``TRADE_PLAN_ISSUED`` events from the JSONL event log and
computes recent performance statistics that are injected into every LLM
adjudication call so Claude can self-calibrate based on what has actually
been working.

Usage::

    ctx = build_performance_context(
        log_path="logs/events.jsonl",
        lookback_days=30,
        few_shot_examples=2,
    )
    # ctx is None when there is insufficient resolved data.
    if ctx:
        prompt_builder.set_performance_context(ctx)

The module has no side-effects and is pure-Python — safe to import anywhere.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from drift.models import SignalEvent
from drift.storage.reader import load_events_from_log

# Outcome labels that count as resolved (exclude open / in-flight signals)
_WIN_OUTCOMES = {"TP1_HIT", "TP2_HIT"}
_LOSS_OUTCOMES = {"STOP_HIT"}
_NEUTRAL_OUTCOMES = {"TIME_STOP", "SESSION_END"}
_RESOLVED_OUTCOMES = _WIN_OUTCOMES | _LOSS_OUTCOMES | _NEUTRAL_OUTCOMES


@dataclass
class FewShotExample:
    """A single resolved signal used as a demonstration for the LLM."""

    event_time_utc: str          # ISO 8601
    setup_type: str
    bias: str                    # LONG or SHORT
    confidence: int
    thesis: str
    outcome: str                 # TP1_HIT / STOP_HIT etc.
    pnl_points: float


@dataclass
class SetupStats:
    """Per-setup-type performance summary."""

    setup_type: str
    total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    avg_pnl_points: float = 0.0


@dataclass
class PerformanceContext:
    """Structured performance summary injected into every LLM system prompt.

    Fields
    ------
    lookback_days:
        How many calendar days of data are included.
    resolved_count:
        Number of fully resolved TRADE_PLAN_ISSUED events in the window.
    overall_win_rate_pct:
        Win rate across all resolved trades (wins / (wins + losses)).
    recent_streak:
        Positive = consecutive wins; negative = consecutive losses; 0 = mixed.
    best_hour_utc:
        UTC hour-of-day with the highest win rate (None if < 2 resolved hours).
    worst_hour_utc:
        UTC hour-of-day with the lowest win rate (None if < 2 resolved hours).
    setup_stats:
        Per-setup-type win rate and average P&L.
    few_shot_examples:
        Up to N recent resolved signals (mix of wins and losses).
    """

    lookback_days: int
    resolved_count: int
    overall_win_rate_pct: float
    recent_streak: int                        # +N = N wins, -N = N losses
    best_hour_utc: int | None                 # 0-23
    worst_hour_utc: int | None                # 0-23
    setup_stats: list[SetupStats] = field(default_factory=list)
    few_shot_examples: list[FewShotExample] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_performance_context(
    log_path: Path | str,
    *,
    lookback_days: int = 30,
    few_shot_examples: int = 2,
) -> PerformanceContext | None:
    """Build a PerformanceContext from the JSONL event log.

    Returns ``None`` if there are fewer than 3 resolved trades in the window —
    not enough data to be informative.

    Args:
        log_path:          Path to the ``events.jsonl`` file.
        lookback_days:     How many calendar days back to include.
        few_shot_examples: How many example signals to surface (each is ~200 tokens).
    """
    events = load_events_from_log(log_path)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)

    resolved = [
        e for e in events
        if e.final_outcome == "TRADE_PLAN_ISSUED"
        and e.replay_outcome is not None
        and e.replay_outcome.get("outcome") in _RESOLVED_OUTCOMES
        and e.event_time >= cutoff
    ]

    if len(resolved) < 3:
        return None

    wins = [e for e in resolved if e.replay_outcome["outcome"] in _WIN_OUTCOMES]
    losses = [e for e in resolved if e.replay_outcome["outcome"] in _LOSS_OUTCOMES]

    total_decisive = len(wins) + len(losses)
    overall_win_rate = round(len(wins) / total_decisive * 100, 1) if total_decisive > 0 else 0.0

    streak = _compute_streak(resolved)
    setup_stats = _compute_setup_stats(resolved)
    best_hour, worst_hour = _compute_best_worst_hours(resolved)
    examples = _pick_few_shot_examples(resolved, few_shot_examples)

    return PerformanceContext(
        lookback_days=lookback_days,
        resolved_count=len(resolved),
        overall_win_rate_pct=overall_win_rate,
        recent_streak=streak,
        best_hour_utc=best_hour,
        worst_hour_utc=worst_hour,
        setup_stats=setup_stats,
        few_shot_examples=examples,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_streak(resolved: list[SignalEvent]) -> int:
    """Return +N for N consecutive wins, -N for N consecutive losses."""
    # Walk the list newest-first
    streak = 0
    for event in reversed(resolved):
        outcome = event.replay_outcome["outcome"]  # type: ignore[index]
        if outcome in _WIN_OUTCOMES:
            if streak < 0:
                break
            streak += 1
        elif outcome in _LOSS_OUTCOMES:
            if streak > 0:
                break
            streak -= 1
        # Neutral outcomes (TIME_STOP, SESSION_END) break the streak
        else:
            break
    return streak


def _compute_setup_stats(resolved: list[SignalEvent]) -> list[SetupStats]:
    """Compute per-setup-type win rate and avg P&L."""
    buckets: dict[str, list[SignalEvent]] = defaultdict(list)
    for event in resolved:
        setup_type = _get_setup_type(event)
        buckets[setup_type].append(event)

    stats: list[SetupStats] = []
    for setup_type, events in sorted(buckets.items()):
        wins = sum(1 for e in events if e.replay_outcome["outcome"] in _WIN_OUTCOMES)
        losses = sum(1 for e in events if e.replay_outcome["outcome"] in _LOSS_OUTCOMES)
        decisive = wins + losses
        win_rate = round(wins / decisive * 100, 1) if decisive > 0 else 0.0
        avg_pnl = round(
            sum(e.replay_outcome.get("pnl_points", 0.0) for e in events) / len(events), 2
        )
        stats.append(SetupStats(
            setup_type=setup_type,
            total=len(events),
            wins=wins,
            losses=losses,
            win_rate_pct=win_rate,
            avg_pnl_points=avg_pnl,
        ))
    return stats


def _compute_best_worst_hours(
    resolved: list[SignalEvent],
) -> tuple[int | None, int | None]:
    """Return (best_utc_hour, worst_utc_hour) by win rate, or (None, None)."""
    hour_wins: dict[int, int] = defaultdict(int)
    hour_decisive: dict[int, int] = defaultdict(int)

    for event in resolved:
        outcome = event.replay_outcome["outcome"]  # type: ignore[index]
        if outcome not in _WIN_OUTCOMES and outcome not in _LOSS_OUTCOMES:
            continue
        hour = event.event_time.hour
        hour_decisive[hour] += 1
        if outcome in _WIN_OUTCOMES:
            hour_wins[hour] += 1

    # Only consider hours with at least 2 decisive trades
    eligible = {h: hour_wins[h] / hour_decisive[h] for h in hour_decisive if hour_decisive[h] >= 2}
    if len(eligible) < 2:
        return None, None

    best = max(eligible, key=eligible.__getitem__)
    worst = min(eligible, key=eligible.__getitem__)
    return best, worst


def _pick_few_shot_examples(
    resolved: list[SignalEvent], count: int
) -> list[FewShotExample]:
    """Pick up to ``count`` examples: prefer a balanced mix of wins and losses."""
    if count <= 0:
        return []

    wins = [e for e in resolved if e.replay_outcome["outcome"] in _WIN_OUTCOMES]
    losses = [e for e in resolved if e.replay_outcome["outcome"] in _LOSS_OUTCOMES]

    # Sort newest-first within each bucket
    wins.sort(key=lambda e: e.event_time, reverse=True)
    losses.sort(key=lambda e: e.event_time, reverse=True)

    # Build a balanced pool: alternating win/loss, newest first
    pool: list[SignalEvent] = []
    half = max(1, count // 2)
    pool.extend(wins[:half])
    pool.extend(losses[:half])
    # If one bucket is smaller, fill from the other
    if len(pool) < count:
        remaining = count - len(pool)
        extras = [e for e in resolved if e not in pool]
        extras.sort(key=lambda e: e.event_time, reverse=True)
        pool.extend(extras[:remaining])

    return [_to_few_shot(e) for e in pool[:count]]


def _to_few_shot(event: SignalEvent) -> FewShotExample:
    plan = event.trade_plan or {}
    parsed = event.llm_decision_parsed or {}
    outcome = event.replay_outcome or {}

    return FewShotExample(
        event_time_utc=event.event_time.isoformat(),
        setup_type=plan.get("setup_type") or parsed.get("setup_type") or "unknown",
        bias=plan.get("bias") or parsed.get("decision") or "unknown",
        confidence=plan.get("confidence") or parsed.get("confidence") or 0,
        thesis=parsed.get("thesis") or "",
        outcome=outcome.get("outcome", "UNKNOWN"),
        pnl_points=outcome.get("pnl_points", 0.0),
    )


def _get_setup_type(event: SignalEvent) -> str:
    plan = event.trade_plan or {}
    parsed = event.llm_decision_parsed or {}
    return plan.get("setup_type") or parsed.get("setup_type") or "unknown"
