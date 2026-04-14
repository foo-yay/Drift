from __future__ import annotations

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
    market_note: str | None = None


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
    snapshot: dict[str, Any] | None = None
    llm_decision_raw: dict[str, Any] | None = None
    llm_decision_parsed: dict[str, Any] | None = None
    pre_gate_report: dict[str, Any] | None = None
    post_gate_report: dict[str, Any] | None = None
    trade_plan: dict[str, Any] | None = None
    final_outcome: str
    final_reason: str
    replay_outcome: dict[str, Any] | None = None  # OutcomeResult if resolved during replay

