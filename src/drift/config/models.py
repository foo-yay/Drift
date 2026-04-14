from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class SessionBlock(BaseModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class AppSection(BaseModel):
    name: str
    timezone: str
    loop_interval_seconds: int = Field(gt=0)
    mode: Literal["paper-live", "replay", "dry-run", "llm-debug"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class InstrumentSection(BaseModel):
    symbol: str = Field(min_length=1)
    allow_long: bool
    allow_short: bool

    @model_validator(mode="after")
    def validate_direction_flags(self) -> "InstrumentSection":
        if not self.allow_long and not self.allow_short:
            msg = "At least one of allow_long or allow_short must be enabled."
            raise ValueError(msg)
        return self


class SessionsSection(BaseModel):
    enabled: bool
    blocks: list[SessionBlock]
    skip_first_n_minutes_after_open: int = Field(ge=0)


class LookbackSection(BaseModel):
    bars_1m: int = Field(gt=0)
    bars_5m: int = Field(gt=0)
    bars_1h: int = Field(gt=0)


class FeaturesSection(BaseModel):
    ema_periods: list[int]
    rsi_period: int = Field(gt=0)
    atr_period: int = Field(gt=0)
    macd_fast: int = Field(gt=0)
    macd_slow: int = Field(gt=0)
    macd_signal: int = Field(gt=0)
    volume_spike_window: int = Field(gt=0)

    @field_validator("ema_periods")
    @classmethod
    def validate_ema_periods(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("ema_periods cannot be empty.")
        if sorted(value) != value:
            raise ValueError("ema_periods must be sorted ascending.")
        if any(period <= 0 for period in value):
            raise ValueError("ema_periods must contain only positive integers.")
        return value


class CalendarSection(BaseModel):
    enabled: bool
    buffer_minutes_before: int = Field(ge=0)
    buffer_minutes_after: int = Field(ge=0)
    filter_countries: list[str]  # e.g. ["USD"]
    cache_ttl_minutes: int = Field(gt=0)


class GatesSection(BaseModel):
    regime_enabled: bool
    min_trend_score: int = Field(ge=0, le=100)
    min_momentum_score: int = Field(ge=0, le=100)
    block_on_extreme_volatility: bool
    cooldown_enabled: bool
    kill_switch_enabled: bool
    kill_switch_path: str


class RiskSection(BaseModel):
    min_confidence: int = Field(ge=0, le=100)
    min_reward_risk: float = Field(gt=0)
    max_signals_per_day: int = Field(gt=0)
    cooldown_minutes: int = Field(ge=0)
    max_stop_points: float = Field(gt=0)
    min_stop_points: float = Field(gt=0)
    atr_stop_floor_mult: float = Field(gt=0)
    atr_target_mult: float = Field(gt=0)
    max_hold_minutes_default: int = Field(gt=0)
    no_trade_during_high_impact_events: bool

    @model_validator(mode="after")
    def validate_stop_window(self) -> "RiskSection":
        if self.max_stop_points <= self.min_stop_points:
            raise ValueError("max_stop_points must be greater than min_stop_points.")
        return self


class StrategySection(BaseModel):
    allowed_setup_types: list[str]
    extension_atr_threshold: float = Field(gt=0)
    chase_buffer_points: float = Field(gt=0)
    structure_buffer_points: float = Field(gt=0)


class LLMSection(BaseModel):
    provider: str
    model: str
    temperature: float = Field(ge=0, le=1)
    timeout_seconds: int = Field(gt=0)
    max_retries: int = Field(ge=0)


class StorageSection(BaseModel):
    use_sqlite: bool
    sqlite_path: str
    jsonl_event_log: str
    csv_signal_log: str


class OutputSection(BaseModel):
    console: bool
    desktop_notifications: bool
    streamlit_dashboard: bool


class AppConfig(BaseModel):
    app: AppSection
    instrument: InstrumentSection
    sessions: SessionsSection
    lookbacks: LookbackSection
    features: FeaturesSection
    risk: RiskSection
    calendar: CalendarSection
    gates: GatesSection
    strategy: StrategySection
    llm: LLMSection
    storage: StorageSection
    output: OutputSection

