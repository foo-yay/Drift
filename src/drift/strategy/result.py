"""Shared result type for all deterministic strategy modules.

Each strategy returns a SetupResult.  When decision == "NO_TRADE" the
entry/stop/target fields are None.  The debug dict always explains which
sub-conditions passed or failed so the operator can audit the logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SetupResult:
    """Output of a deterministic strategy scanner.

    Mirrors the TradePlan shape closely so app.py can convert it directly.
    """

    decision: str                           # "LONG" | "SHORT" | "NO_TRADE"
    setup_type: str                         # e.g. "liquidity_sweep"
    confidence: int = 0                     # 0–100 deterministic score
    context_trigger: str = ""               # what fired the trigger
    confirmation_type: str = ""             # pin_bar | momentum_fvg

    # Entry / risk levels (None when NO_TRADE)
    entry_min: float | None = None
    entry_max: float | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    reward_risk_ratio: float | None = None

    thesis: str = ""
    invalidation_conditions: list[str] = field(default_factory=list)
    no_trade_reason: str = ""               # populated when decision == NO_TRADE

    # Sub-condition flags for full auditability
    debug: dict[str, object] = field(default_factory=dict)
