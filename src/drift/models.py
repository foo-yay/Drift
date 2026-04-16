from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CalendarEvent(BaseModel):
    """A scheduled economic event that may affect trading conditions."""

    title: str
    country: str
    event_time: datetime
    impact: Literal["High", "Medium", "Low", "Holiday"]
    forecast: str | None = None
    previous: str | None = None

    @property
    def is_high_impact(self) -> bool:
        return self.impact == "High"

    def minutes_until(self, now: datetime) -> float:
        """Signed minutes until this event from `now` (negative = already passed)."""
        delta = self.event_time - now
        return delta.total_seconds() / 60


class Bar(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str
    symbol: str

    @model_validator(mode="after")
    def validate_ohlc(self) -> "Bar":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be greater than or equal to open, close, and low.")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be less than or equal to open, close, and high.")
        return self


class MarketSnapshot(BaseModel):
    as_of: datetime
    symbol: str
    last_price: float
    session: str
    bars_1m_count: int = Field(ge=0)
    bars_5m_count: int = Field(ge=0)
    bars_1h_count: int = Field(ge=0)
    trend_score: int = Field(ge=0, le=100)
    momentum_score: int = Field(ge=0, le=100)
    volatility_score: int = Field(ge=0, le=100)
    extension_risk: int = Field(ge=0, le=100)
    structure_quality: int = Field(ge=0, le=100)
    pullback_quality: int = Field(ge=0, le=100)
    breakout_quality: int = Field(ge=0, le=100)
    mean_reversion_risk: int = Field(ge=0, le=100)
    session_alignment: int = Field(ge=0, le=100)
    short_trend_state: str
    medium_trend_state: str
    momentum_state: str
    volatility_regime: str
    order_blocks: list[dict] = Field(default_factory=list)
    rejection_blocks: list[dict] = Field(default_factory=list)
    atr: float | None = None
    volume_imbalance: float | None = None
    market_note: str | None = None


class WatchCondition(BaseModel):
    """A price/indicator level the LLM wants to monitor after a NO_TRADE decision.

    When the condition is met by the fast-poll watcher, a full LLM cycle is
    triggered automatically so the opportunity is not missed.

    condition_type options:
      price_above  — trigger when last_price >= value
      price_below  — trigger when last_price <= value
      rsi_above    — trigger when 14-period RSI on 1m bars >= value
      rsi_below    — trigger when 14-period RSI on 1m bars <= value
    """

    condition_type: Literal["price_above", "price_below", "rsi_above", "rsi_below"]
    value: float
    description: str  # human-readable: "pullback to support at 21,000"
    expires_minutes: int = Field(default=60, ge=5, le=480)


class LLMDecision(BaseModel):
    decision: Literal["LONG", "SHORT", "NO_TRADE"]
    confidence: int = Field(ge=0, le=100)
    setup_type: str
    thesis: str
    entry_style: str
    entry_zone: list[float]
    invalidation_hint: str
    hold_minutes: int = Field(ge=1, le=120)
    do_not_trade_if: list[str]
    watch_conditions: list[WatchCondition] = Field(default_factory=list)


class GateResult(BaseModel):
    gate_name: str
    passed: bool
    reason: str


class GateReport(BaseModel):
    all_passed: bool
    results: list[GateResult]


class TradePlan(BaseModel):
    generated_at: datetime
    symbol: str
    bias: Literal["LONG", "SHORT"]
    setup_type: str
    confidence: int = Field(ge=0, le=100)
    entry_min: float
    entry_max: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    reward_risk_ratio: float = Field(gt=0)
    max_hold_minutes: int = Field(gt=0)
    thesis: str
    invalidation_conditions: list[str]
    operator_instructions: list[str]
    do_not_trade_if: list[str]
    chase_above_below: float | None = None

    @model_validator(mode="after")
    def validate_entry_range(self) -> "TradePlan":
        if self.entry_min > self.entry_max:
            raise ValueError("entry_min must be less than or equal to entry_max.")
        return self


class SignalEvent(BaseModel):
    event_time: datetime
    symbol: str
    source: Literal["live", "replay", "sandbox"] = "live"
    signal_key: str | None = None          # deterministic dedup key — set on first write
    snapshot: dict[str, Any] | None = None
    llm_decision_raw: dict[str, Any] | None = None
    llm_decision_parsed: dict[str, Any] | None = None
    pre_gate_report: dict[str, Any] | None = None
    post_gate_report: dict[str, Any] | None = None
    trade_plan: dict[str, Any] | None = None
    final_outcome: str
    final_reason: str
    replay_outcome: dict[str, Any] | None = None  # OutcomeResult if resolved during replay

    def compute_signal_key(self) -> str:
        """Return a 16-char deterministic dedup key based on symbol, snapshot time, and source.

        Uses ``snapshot["as_of"]`` when available (the exact market moment), falling back to
        ``event_time`` so events without a snapshot (BLOCKED cycles) are still keyed uniquely.
        """
        as_of = (self.snapshot or {}).get("as_of") or self.event_time.isoformat()
        raw = f"{self.symbol}|{as_of}|{self.source}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def ensure_signal_key(self) -> "SignalEvent":
        """Return a copy of self with signal_key populated if it was missing."""
        if self.signal_key:
            return self
        return self.model_copy(update={"signal_key": self.compute_signal_key()})

