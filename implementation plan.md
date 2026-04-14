# MNQ AI Signal Bot Creation Document

## Purpose

Build a standalone local Python application for macOS that generates **manual trade alerts** for MNQ-style directional setups. The app does **not** place trades. It analyzes market context, produces a proposed trade plan, and outputs simple execution instructions the operator can manually enter into Robinhood or another broker.

The system should prioritize:

- safety over frequency
- deterministic controls over opaque autonomy
- traceability over cleverness
- local operability on a Mac
- clear operator instructions

This is an **AI-assisted signal and trade-plan generator**, not an autonomous execution bot.

---

# 1. Product Definition

## 1.1 Core objective

Every cycle, the app should answer:

1. Is there a valid setup right now?
2. If yes, is it LONG, SHORT, or NO TRADE?
3. What is the entry logic?
4. What is the stop-loss?
5. What is the take-profit?
6. What conditions would invalidate the trade?
7. Should the operator ignore the setup because risk or market conditions are poor?

## 1.2 Final operator output

The operator-facing output must be simple enough to follow manually.

Example:

- Instrument: MNQ
- Bias: LONG
- Entry zone: 19502.00 to 19506.00
- Stop loss: 19480.00
- Take profit 1: 19528.00
- Take profit 2: 19544.00
- Max hold: 25 minutes
- Thesis: Trend continuation above VWAP with strong momentum and no major opposing pressure
- Invalid if: price loses VWAP and closes below 19494 on the 1-minute bar
- Confidence: 72/100
- Action: Wait for pullback into entry zone. Do not chase above 19510.
- Risk note: Skip if spread widens or move extends before entry.

## 1.3 Non-goals for version 1

The first version should **not**:

- place trades automatically
- connect directly to Robinhood for execution
- trade multiple instruments
- run multiple strategies
- optimize itself live
- generate highly discretionary narrative outputs
- rely on fragile intrabar micro-timing

---

# 2. Product Philosophy

## 2.1 System design principles

The application should be designed around the following rules:

### Rule 1: The AI proposes, hard rules dispose
The LLM may propose a direction or explain the setup, but deterministic controls must decide whether the signal is eligible.

### Rule 2: Risk logic must be deterministic
Stop placement, position sizing guidance, invalidation logic, session filters, and kill switches should be rules-based wherever possible.

### Rule 3: Manual execution is a feature, not a flaw
The operator remains the final gate. The app is designed to reduce bad discretionary trades, not eliminate human agency.

### Rule 4: Every signal must be auditable
Each signal must be logged with full market context, model outputs, gating results, and final recommendation.

### Rule 5: No signal is better than a bad signal
The system should be comfortable outputting NO TRADE most of the time.

---

# 3. High-Level System Architecture

## 3.1 Major subsystems

The application should be broken into these layers:

1. **Market data ingestion layer**
2. **Bar builder / normalization layer**
3. **Feature engineering layer**
4. **Regime and setup scoring layer**
5. **AI decision layer**
6. **Deterministic gate layer**
7. **Trade plan constructor**
8. **Operator output layer**
9. **Persistence and logging layer**
10. **Configuration and safety layer**

## 3.2 Recommended runtime model

A single local Python app should run in a loop on the user’s Mac.

Suggested runtime modes:

- `paper-live`: uses live data and emits real-time signals
- `replay`: replays historical candles for testing
- `dry-run`: runs logic without LLM calls
- `llm-debug`: prints prompt and parsed response details

---

# 4. Recommended Project Scope for Version 1

## 4.1 Narrow initial scope

Version 1 should support:

- one instrument only
- one strategy family only
- one cycle interval only
- one LLM provider only
- manual execution only
- long / short / no trade only

## 4.2 Suggested first strategy profile

Use a slower discretionary-assist profile:

- cycle every 15 minutes (revised — see section 23.2)
- use 15-minute and 1-hour bars as primary decision frames
- use daily and 4H bars as trend context
- avoid sub-second order book logic in version 1
- avoid live reversal logic in version 1
- avoid dynamic self-generated SL/TP in version 1 unless bounded by hard rules

---

# 5. Data and Inputs

## 5.1 Required data inputs

The app should ingest:

- live or delayed price data for MNQ or equivalent proxy
- OHLCV bars for 1-minute, 5-minute, and 1-hour timeframes
- session information
- optional volatility context
- optional market breadth or benchmark context

## 5.2 Recommended approach for version 1

For the initial build, use the most stable and legally accessible feed the operator can reliably obtain on a Mac. The app design should abstract the data source behind a provider interface.

## 5.3 Data provider abstraction

Create a provider interface such as:

- `get_latest_quote()`
- `get_recent_bars(symbol, timeframe, lookback)`
- `get_session_status()`
- `is_market_open()`

This allows the data vendor to be swapped later without changing the strategy core.

---

# 6. Feature Engineering

## 6.1 Base indicators

The app should compute, at minimum:

- EMA 9
- EMA 21
- EMA 50
- VWAP
- RSI
- MACD histogram
- ATR(14)
- rolling high / low structure
- session high / low
- volume spike metrics
- distance from VWAP
- candle body and wick characteristics

## 6.2 Derived context fields

The system should also derive:

- short-term trend state
- medium-term trend state
- momentum state
- overextension state
- mean-reversion risk
- volatility regime
- support / resistance proximity
- distance to session extremes
- whether the setup is early, mature, or exhausted

## 6.3 Feature design rule

All features should be computed deterministically and stored in a normalized market snapshot object before any AI call is made.

---

# 7. Regime and Setup Scoring

## 7.1 Purpose

Before invoking the LLM, the app should build a deterministic scorecard. This reduces prompt vagueness and supports hard gating.

## 7.2 Suggested score dimensions

Use numeric scores for:

- trend
- momentum
- volatility
- extension
- structure quality
- breakout quality
- pullback quality
- mean-reversion danger
- session alignment

## 7.3 Example output

```json
{
  "trend_score": 68,
  "momentum_score": 54,
  "volatility_score": 41,
  "extension_risk": 72,
  "pullback_quality": 63,
  "breakout_quality": 35,
  "session_alignment": 77
}
```

## 7.4 Important rule

A setup should never depend on the LLM to infer raw technical structure from messy data alone. The deterministic system must present structured context first.

---

# 8. AI Decision Layer

## 8.1 Role of the LLM

The LLM should be used for **structured adjudication**, not unconstrained creativity.

> **Implementation note:** Provider is Anthropic Claude (`claude-sonnet-4-6`). See section 23.1.

Its job is to assess the precomputed market snapshot and return one of:

- LONG
- SHORT
- NO_TRADE

It may also provide:

- short thesis
- setup type
- entry preference
- invalidation cue
- hold horizon estimate

## 8.2 What the LLM should not control in V1

The LLM should not have final authority over:

- whether a trade is allowed
- final stop-loss distance
- final take-profit targets
- size recommendations
- session eligibility
- kill-switch behavior

## 8.3 Required response schema

Force strict JSON output.

Example:

```json
{
  "decision": "LONG",
  "confidence": 72,
  "setup_type": "pullback_continuation",
  "thesis": "Bullish trend intact above VWAP with constructive pullback into EMA cluster.",
  "entry_style": "buy_pullback",
  "entry_zone": [19502.0, 19506.0],
  "invalidation_hint": "1m close below VWAP and prior pullback low",
  "hold_minutes": 20,
  "do_not_trade_if": [
    "price extends beyond entry zone before confirmation",
    "next 1m candle closes below VWAP"
  ]
}
```

## 8.4 Determinism requirements

- low temperature
- schema validation
- retry on malformed JSON
- hard fail to NO_TRADE if parsing fails

---

# 9. Deterministic Gate Layer

## 9.1 Purpose

This is the most important layer. A signal only survives if it passes deterministic gating.

## 9.2 Required gates

### Gate 1: Session gate
Allowed trading window only.

### Gate 2: Data freshness gate
Reject if bars are stale or incomplete.

### Gate 3: Regime gate
Reject if market conditions do not support the proposed setup type.

### Gate 4: Extension gate
Reject if price is too extended relative to ATR, VWAP, or recent structure.

### Gate 5: Minimum reward-to-risk gate
Reject if projected R:R is below threshold.

### Gate 6: Structural stop sanity gate
Reject if stop would be too tight or too wide.

### Gate 7: Trade frequency gate
Reject if too many signals have already fired recently.

### Gate 8: Cooldown gate
Reject if recent stop-out or invalidation suggests waiting.

### Gate 9: Daily operator kill-switch gate
Reject if the user has disabled signal generation or daily cap hit.

## 9.3 Suggested initial thresholds

These should be configurable, not hardcoded into logic.

Example:

- allowed session: 9:40 AM to 11:30 AM ET and 1:30 PM to 3:30 PM ET
- min confidence: 65
- minimum R:R: 1.8
- max signals per day: 3
- cooldown after invalid signal: 15 minutes
- max stop distance: configurable in points
- no entries during first 10 minutes after open

---

# 10. Stop Loss and Protection Logic

## 10.1 Design rule

Stop losses must be deterministic and structure-based.

The LLM may describe why the trade is valid, but stop placement should be generated from rules.

## 10.2 Stop logic hierarchy

For a LONG:

1. find nearest valid structural low
2. compare with ATR-based minimum distance
3. place stop slightly beyond structural invalidation
4. reject if stop becomes too wide

For a SHORT:

1. find nearest valid structural high
2. compare with ATR-based minimum distance
3. place stop slightly beyond structural invalidation
4. reject if stop becomes too wide

## 10.3 Example stop calculation

LONG stop candidate:

- pullback low = 19486.50
- safety buffer = 2.00 points
- ATR floor distance = 9.00 points
- chosen stop = max(structural logic, minimum distance logic according to strategy rules)

## 10.4 Protections to include in operator output

Every trade output must specify:

- exact stop price
- why that stop exists
- whether the stop is structural or volatility-based
- maximum acceptable chase level
- time-based invalidation
- condition-based invalidation

---

# 11. Take-Profit Logic

## 11.1 Design rule

Take-profit targets should also be deterministic.

## 11.2 Recommended target logic

Use one or more of:

- fixed multiple of stop distance
- session structure target
- VWAP reversion target
- prior swing target
- measured move target

## 11.3 Suggested v1 output

Provide:

- TP1
- TP2
- whether the setup is all-out at TP1 or scale-out in theory
- ideal manual management note

Because the operator is trading manually, the instructions must be simple.

---

# 12. Signal Construction Rules

## 12.1 A valid signal must contain

- instrument
- timestamp
- bias
- setup type
- entry zone
- stop loss
- TP1
- TP2 if applicable
- risk-reward estimate
- confidence
- thesis
- invalidation conditions
- operator action instructions

## 12.2 Signal quality standard

If any required component is missing, the app should emit NO_TRADE instead of an incomplete signal.

---

# 13. Operator UX Requirements

## 13.1 Output channels

Version 1 should support at least:

- terminal console output
- structured log file
- optional local HTML dashboard
- optional desktop notification

## 13.2 Operator-first message format

Each signal should be displayed in plain language first, with technical details underneath.

### Example display

## SIGNAL
Bias: SHORT  
Confidence: 70  
Setup: failed breakout reversion  

Entry: 19488.00 to 19484.00  
Stop: 19502.00  
TP1: 19466.00  
TP2: 19454.00  
Max Hold: 18 min  

Instructions:
- Wait for price to trade back into the entry zone.
- Do not enter if price already breaks below 19480 before you are in.
- In Robinhood, use the stop at 19502.00.
- Skip entirely if the next 1-minute candle closes above VWAP.

Reason:
Bearish failure at prior resistance with weakening momentum and rejection from extension.

## 13.3 No ambiguity rule

The output should tell the operator what to do, what not to do, and when to ignore the signal.

---

# 14. Logging, Persistence, and Audit Trail

## 14.1 Required logs

The app must log:

- market snapshot used for decision
- computed indicators
- regime scores
- raw LLM prompt payload
- raw LLM response
- parsed LLM response
- gate pass/fail results
- final signal or NO_TRADE reason
- operator acknowledgment if manually recorded

## 14.2 Storage format

Use simple local storage first:

- JSONL for event logs
- CSV for signal summary table
- SQLite for richer local querying if desired

## 14.3 Why this matters

Without complete logging, the system cannot be debugged, trusted, or improved.

---

# 15. Safety Controls and Risk Controls

## 15.1 Hard safety requirements

The application must include:

- daily max signal cap
- cooldown after stop-like invalidation
- configurable session windows
- stale data detection
- missing data fail-safe
- malformed LLM response fail-safe
- configuration validation on startup
- kill switch file or command
- optional “no signals during major event windows” flag

## 15.2 Risk philosophy

The bot should help the operator avoid low-quality trades. It should not encourage overtrading.

## 15.3 Recommended operator protections

The final output should remind the operator:

- not to chase beyond the defined entry zone
- not to trade if stop cannot be honored
- not to trade if risk exceeds personal daily plan
- not to take multiple conflicting signals in a row

---

# 16. Proposed Folder Structure

```text
mnq_signal_bot/
├── app.py
├── config/
│   ├── settings.yaml
│   └── prompts.yaml
├── data/
│   ├── providers/
│   │   ├── base.py
│   │   └── provider_x.py
│   ├── bar_builder.py
│   └── session.py
├── features/
│   ├── indicators.py
│   ├── structure.py
│   └── snapshot.py
├── scoring/
│   ├── regime.py
│   └── setup_quality.py
├── ai/
│   ├── client.py
│   ├── schemas.py
│   ├── prompts.py
│   └── parser.py
├── gates/
│   ├── session_gate.py
│   ├── freshness_gate.py
│   ├── extension_gate.py
│   ├── rr_gate.py
│   ├── cooldown_gate.py
│   └── orchestrator.py
├── planning/
│   ├── stop_engine.py
│   ├── target_engine.py
│   └── signal_builder.py
├── output/
│   ├── console.py
│   ├── notifier.py
│   └── dashboard.py
├── storage/
│   ├── logger.py
│   ├── events.py
│   └── sqlite_store.py
├── replay/
│   ├── runner.py
│   └── evaluator.py
├── utils/
│   ├── config.py
│   ├── clock.py
│   └── validation.py
└── tests/
```

---

# 17. Configuration Design

## 17.1 All important controls should be configurable

Examples:

- symbol
- timeframes
- session windows
- min confidence
- minimum R:R
- ATR multipliers
- max stop size
- max signals per day
- cooldown minutes
- whether shorts are allowed
- whether longs are allowed
- event blackout windows

## 17.2 Config rule

Strategy behavior must live in config where possible, not buried in code.

---

# 18. Recommended Internal Objects

## 18.1 MarketSnapshot
A normalized object containing all market state and computed fields.

## 18.2 LLMDecision
Strict parsed object from the model.

## 18.3 GateResult
Contains pass/fail and reason per gate.

## 18.4 TradePlan
Final operator-facing signal object.

## 18.5 SignalEvent
Persistent record of the entire decision lifecycle.

---

# 19. App Control Flow

## 19.1 Main loop

1. load config
2. validate environment
3. fetch latest data
4. build bars and snapshot
5. compute indicators and scores
6. run pre-LLM gates
7. call LLM with structured snapshot
8. parse and validate response
9. build deterministic stop and targets
10. run post-LLM trade gates
11. emit signal or NO_TRADE
12. log everything
13. sleep until next cycle

## 19.2 Pre-LLM gating

Do not waste model calls if:

- market closed
- stale data
- not in allowed session
- insufficient lookback
- volatility too abnormal

## 19.3 Post-LLM gating

After the model responds, validate:

- decision allowed by mode
- confidence above threshold
- stop valid
- target valid
- reward-risk valid
- setup not too extended

---

# 20. Manual Robinhood Execution Requirements

## 20.1 The app is not broker-native

The app’s responsibility is to generate a precise trade plan.

## 20.2 Operator instruction standard

For every signal, include:

- what symbol to trade
- what direction
- what entry range is acceptable
- where stop goes
- where target goes
- when not to take it
- when the idea expires

## 20.3 Example operator instruction block

```text
MANUAL EXECUTION CHECKLIST
1. Confirm instrument and direction.
2. Enter only inside the entry zone.
3. Immediately place stop at the listed stop price.
4. Place target at TP1, or manage according to your plan.
5. Skip if price already moved through the zone.
6. Skip if invalidation condition triggers before entry.
7. Exit the trade if time-based expiration is reached and thesis has not resolved.
```

---

# 21. Testing and Validation Plan

## 21.1 Minimum required testing before real use

The project is not ready until it passes:

- unit tests for indicators and stop/target logic
- schema tests for LLM responses
- replay tests on historical sessions
- logging integrity tests
- stale data and malformed response failover tests

## 21.2 Replay framework requirement

A replay runner should allow the app to step through historical bars as though live. This is essential.

## 21.3 Core benchmark question

The system must answer:

Does the LLM improve signal quality versus a deterministic baseline using the same inputs?

---

# 22. Metrics to Track

## 22.1 Signal metrics

Track:

- number of signals per day
- percent no-trade cycles
- signal direction distribution
- average confidence
- average stop distance
- average projected R:R

## 22.2 Outcome metrics

Track:

- hit rate
- average max favorable excursion
- average max adverse excursion
- TP1 hit rate
- full invalidation rate
- time-to-resolution

## 22.3 Quality metrics

Track:

- signals blocked by each gate
- malformed LLM response rate
- stale data incidents
- config override incidents

---

# 23. Confirmed Technical Decisions (April 2026)

The following decisions were made and locked in after the initial build phases were complete. They supersede earlier assumptions in sections above where they conflict.

## 23.1 LLM provider and model

- **Provider:** Anthropic Claude (replaces OpenAI assumption from earlier sections)
- **Model:** `claude-sonnet-4-6`
- **Rationale:** Structured adjudication on a pre-computed snapshot does not need Opus-level reasoning. Sonnet is ~5x faster and cheaper, which matters at 15-minute cadence. Low temperature + strict JSON schema enforces the quality ceiling the task needs.
- **SDK:** `anthropic` Python package, structured output via tool-use or text + schema validation
- **Config key:** `llm.provider: anthropic`, `llm.model: claude-sonnet-4-6`

## 23.2 Poll cadence

- **Changed from 60 seconds → 15 minutes**
- Aligns with the shortest tracked timeframe (15M bars). Data does not meaningfully change within a 15M bar window, so sub-15M polling produces no new signal and wastes LLM calls.
- 15-minute cadence = ~26 LLM calls per full RTH session day.

## 23.3 Feature engineering additions (order blocks)

The feature layer will be extended with ICT/smart-money concepts as additional `FeatureComputer` modules:

- **Order blocks** (`features/order_blocks.py`): Detect candles that caused large price displacement (BOS/CHoCH zones). Mark the body range as a high-interest zone. Track whether price has returned to fill it.
- **Rejection blocks** (`features/rejection_blocks.py`): Detect candles with significant wicks on both sides or heavy rejection from a level. These feed into the invalidation logic in the prompt.

These are computed deterministically from OHLC using pure pandas and added to `MarketSnapshot` as zone lists. The LLM receives them as structured fields, not raw bar data.

The original indicator set (EMAs, VWAP, RSI, ATR, MACD, volume) is retained in full.

## 23.4 Stop First Win gate — deferred

A daily P&L gate ("halt signals once session is green") is architecturally planned but not built in Phase 4. It requires trade outcome tracking. The gate stub is reserved in the gate layer for Phase 5+ once execution context exists.

## 23.5 Auto-execution extensibility

The system will include an `ExecutionAdapter` interface in `planning/` with:
- `NullAdapter` — no-op (current behavior, always active in V1)
- `NinjaTraderAdapter` stub — writes signal file to a watched directory

This seam means integrating NinjaTrader or another broker later requires only implementing the adapter, not restructuring the signal pipeline.

---

# 24. Recommended Build Order

## Phase 1: Foundation

- project scaffold
- config loader
- data provider abstraction
- bar fetch and normalization
- indicator engine
- snapshot object

## Phase 2: Deterministic core

- regime scoring
- stop engine
- target engine
- pre/post gate orchestration
- console output
- structured logging

## Phase 3: AI layer

- prompt builder
- schema parser
- LLM client
- retry/error handling
- decision integration

## Phase 4: Replay and evaluation

- historical replay runner
- bar-by-bar cursor stepping, session label classification
- signal evaluation logic (gate layer + mock/live LLM)
- outcome resolution: TP1/TP2/STOP/TIME_STOP/SESSION_END annotated per signal
- win rate and P&L tracking in ReplaySummary
- summary metrics printed after each session replay

## Phase 5: Operator polish

- local dashboard
- desktop alerts
- signal summary reports

---

## Phase 6: Streamlit replay GUI (deferred — post Phase 4)

**Decision (April 2026):** Build the terminal-based replay engine first (Phase 4). Once the engine is proven, bolt a Streamlit UI on top.

**Why Streamlit:**
- Pure Python — no JavaScript, no separate frontend project
- Candlestick chart (Plotly) with entry zone, stop, and TP levels drawn directly on the chart
- Step through historical bars with a slider or prev/next buttons
- Side panel shows the gate report and LLM decision for the selected bar
- Summary table of all signals that fired across the session with outcome annotations
- Runs locally (`streamlit run ...`) — nothing goes to the internet
- Adds `streamlit` and `plotly` to deps

**What to reuse from Phase 4:**
- `replay/engine.py` — the bar-feeding loop runs unchanged; Streamlit is just a new frontend calling it
- `FeatureEngine`, `GateRunner`, `MockLLMClient` / `LLMClient`, `TradePlanBuilder` — all unchanged
- `SignalEvent` log — Streamlit reads the JSONL log and renders it visually

**Entry point:** `streamlit run src/drift/replay/streamlit_app.py` or a `drift-replay` CLI alias

**Do not start this until:** Phase 4 replay engine is complete and producing correct signal logs.

---

# 24. What the AI Coding Assistant Should Build First

When handing this document to an AI coder, the first request should be:

Build a local Python CLI app that:

- loads configuration from YAML
- fetches market data through an abstract provider interface
- computes a normalized market snapshot
- calculates indicators and regime scores
- runs deterministic gating
- calls an LLM for LONG/SHORT/NO_TRADE adjudication via strict JSON schema
- constructs a deterministic trade plan with stop and target
- prints a clear manual trade instruction block
- logs all decisions locally

Do **not** include auto-execution or broker integration in the first build.

---

# 25. Final Product Standard

A successful version 1 is not “an AI that trades.”

A successful version 1 is:

- a stable Python app
- running locally on a Mac
- producing low-frequency, clearly structured trade plans
- with deterministic protections
- with strong logs
- with enough transparency that the operator can trust, reject, or improve its outputs

---

# 26. Plain-English Summary

This application should behave like a disciplined analyst sitting beside the operator.

It should:

- review the market every minute
- identify only high-quality setups
- explain the trade simply
- define exact entry, stop, target, and invalidation
- output NO TRADE when conditions are poor
- never force action
- never rely on vague intuition alone

That is the correct foundation for a manual AI-assisted trading tool.


---

# 27. Implementation-Ready Technical Specification

## 27.1 Recommended Python stack

Use Python 3.11 or 3.12.

Recommended packages:

### Core app
- `pydantic` for typed models and validation
- `pyyaml` for config loading
- `rich` for terminal output
- `typer` for CLI
- `pandas` for tabular time-series handling
- `numpy` for numerical operations
- `sqlalchemy` or built-in `sqlite3` for local persistence
- `tenacity` for retries
- `python-dotenv` for local environment loading

### Market data / networking
- `httpx` for REST calls
- `websockets` for streaming if needed
- provider-specific SDK only if necessary

### Technical analysis
Prefer implementing indicators directly where simple. If a library is desired, use one of:
- `pandas-ta`
- `ta`

Do not build the system around a TA library that becomes a hard dependency for everything.

### LLM integration
Keep provider-agnostic behind an interface.
Possible SDKs:
- `openai`
- `anthropic`

### Optional UI
- `streamlit` for a quick local dashboard

### Dev tooling
- `pytest`
- `pytest-cov`
- `ruff`
- `mypy`
- `pre-commit`

---

## 27.2 Recommended environment layout

### Local environment variables
Use a `.env` file for secrets only.

Example:

```env
LLM_PROVIDER=openai
LLM_API_KEY=your_key_here
DATA_PROVIDER=provider_x
DATA_API_KEY=your_data_key_here
APP_ENV=local
TZ=America/New_York
```

### Config rule
Secrets go in environment variables. Strategy behavior goes in YAML config.

---

## 27.3 Example settings.yaml

```yaml
app:
  name: mnq-signal-bot
  timezone: America/New_York
  loop_interval_seconds: 60
  mode: paper-live
  log_level: INFO

instrument:
  symbol: MNQ
  allow_long: true
  allow_short: true

sessions:
  enabled: true
  blocks:
    - start: "09:40"
      end: "11:30"
    - start: "13:30"
      end: "15:30"
  skip_first_n_minutes_after_open: 10

lookbacks:
  bars_1m: 180
  bars_5m: 120
  bars_1h: 72

features:
  ema_periods: [9, 21, 50]
  rsi_period: 14
  atr_period: 14
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  volume_spike_window: 20

risk:
  min_confidence: 65
  min_reward_risk: 1.8
  max_signals_per_day: 3
  cooldown_minutes: 15
  max_stop_points: 30.0
  min_stop_points: 6.0
  atr_stop_floor_mult: 0.8
  atr_target_mult: 1.8
  max_hold_minutes_default: 25
  no_trade_during_high_impact_events: false

strategy:
  allowed_setup_types:
    - pullback_continuation
    - breakout_continuation
    - failed_breakout_reversion
  extension_atr_threshold: 1.2
  chase_buffer_points: 4.0
  structure_buffer_points: 2.0

llm:
  provider: openai
  model: gpt-5.4-thinking
  temperature: 0.1
  timeout_seconds: 20
  max_retries: 2

storage:
  use_sqlite: true
  sqlite_path: data/local.db
  jsonl_event_log: logs/events.jsonl
  csv_signal_log: logs/signals.csv

output:
  console: true
  desktop_notifications: false
  streamlit_dashboard: false
```

---

## 27.4 Recommended prompts.yaml structure

```yaml
system_prompt: |
  You are a disciplined futures trade adjudication engine.
  Your task is to evaluate a structured market snapshot and return only one of: LONG, SHORT, NO_TRADE.
  You must be selective.
  You are not allowed to invent data.
  You must return valid JSON matching the required schema.
  Favor NO_TRADE when the setup is unclear, extended, conflicting, or poor reward-to-risk.

decision_rules: |
  Prefer continuation entries when trend and momentum align and extension risk is moderate.
  Prefer failed breakout reversion only when rejection and structure are clear.
  Reject low-quality chop.
  Reject late entries.
  Reject setups with obvious invalidation ambiguity.

json_schema_hint: |
  Return fields:
  decision, confidence, setup_type, thesis, entry_style, entry_zone,
  invalidation_hint, hold_minutes, do_not_trade_if.
```

---

## 27.5 Required Pydantic models

### AppConfig
```python
from pydantic import BaseModel
from typing import List, Literal

class SessionBlock(BaseModel):
    start: str
    end: str

class AppSection(BaseModel):
    name: str
    timezone: str
    loop_interval_seconds: int
    mode: Literal["paper-live", "replay", "dry-run", "llm-debug"]
    log_level: str

class InstrumentSection(BaseModel):
    symbol: str
    allow_long: bool
    allow_short: bool

class SessionsSection(BaseModel):
    enabled: bool
    blocks: List[SessionBlock]
    skip_first_n_minutes_after_open: int

class LookbackSection(BaseModel):
    bars_1m: int
    bars_5m: int
    bars_1h: int

class FeaturesSection(BaseModel):
    ema_periods: List[int]
    rsi_period: int
    atr_period: int
    macd_fast: int
    macd_slow: int
    macd_signal: int
    volume_spike_window: int

class RiskSection(BaseModel):
    min_confidence: int
    min_reward_risk: float
    max_signals_per_day: int
    cooldown_minutes: int
    max_stop_points: float
    min_stop_points: float
    atr_stop_floor_mult: float
    atr_target_mult: float
    max_hold_minutes_default: int
    no_trade_during_high_impact_events: bool

class StrategySection(BaseModel):
    allowed_setup_types: List[str]
    extension_atr_threshold: float
    chase_buffer_points: float
    structure_buffer_points: float

class LLMSection(BaseModel):
    provider: str
    model: str
    temperature: float
    timeout_seconds: int
    max_retries: int

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
    strategy: StrategySection
    llm: LLMSection
    storage: StorageSection
    output: OutputSection
```

### Bar model
```python
from pydantic import BaseModel
from datetime import datetime

class Bar(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str
    symbol: str
```

### MarketSnapshot
```python
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class MarketSnapshot(BaseModel):
    as_of: datetime
    symbol: str
    last_price: float
    session: str
    bars_1m_count: int
    bars_5m_count: int
    bars_1h_count: int

    ema_9_1m: float
    ema_21_1m: float
    ema_50_1m: float
    vwap_1m: float
    rsi_1m: float
    macd_hist_1m: float
    atr_1m: float

    ema_9_5m: float
    ema_21_5m: float
    ema_50_5m: float
    rsi_5m: float
    atr_5m: float

    session_high: float
    session_low: float
    prior_swing_high: Optional[float] = None
    prior_swing_low: Optional[float] = None
    distance_from_vwap: float
    distance_from_session_high: float
    distance_from_session_low: float
    volume_spike_ratio: float

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
    market_note: Optional[str] = None
```

### LLMDecision
```python
from pydantic import BaseModel, Field
from typing import List, Literal

class LLMDecision(BaseModel):
    decision: Literal["LONG", "SHORT", "NO_TRADE"]
    confidence: int = Field(ge=0, le=100)
    setup_type: Literal[
        "pullback_continuation",
        "breakout_continuation",
        "failed_breakout_reversion",
        "none"
    ]
    thesis: str
    entry_style: Literal["buy_pullback", "sell_rip", "breakout", "reversion", "none"]
    entry_zone: List[float]
    invalidation_hint: str
    hold_minutes: int = Field(ge=1, le=120)
    do_not_trade_if: List[str]
```

### GateResult and GateReport
```python
from pydantic import BaseModel
from typing import List

class GateResult(BaseModel):
    gate_name: str
    passed: bool
    reason: str

class GateReport(BaseModel):
    all_passed: bool
    results: List[GateResult]
```

### TradePlan
```python
from pydantic import BaseModel
from typing import List, Literal, Optional
from datetime import datetime

class TradePlan(BaseModel):
    generated_at: datetime
    symbol: str
    bias: Literal["LONG", "SHORT"]
    setup_type: str
    confidence: int
    entry_min: float
    entry_max: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: Optional[float] = None
    reward_risk_ratio: float
    max_hold_minutes: int
    thesis: str
    invalidation_conditions: List[str]
    operator_instructions: List[str]
    do_not_trade_if: List[str]
    chase_above_below: Optional[float] = None
```

### SignalEvent
```python
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime

class SignalEvent(BaseModel):
    event_time: datetime
    symbol: str
    snapshot: Dict[str, Any]
    llm_decision_raw: Optional[Dict[str, Any]] = None
    llm_decision_parsed: Optional[Dict[str, Any]] = None
    pre_gate_report: Dict[str, Any]
    post_gate_report: Dict[str, Any]
    trade_plan: Optional[Dict[str, Any]] = None
    final_outcome: str
    final_reason: str
```

---

## 27.6 Required provider interface

```python
from abc import ABC, abstractmethod
from typing import List

class MarketDataProvider(ABC):
    @abstractmethod
    async def get_recent_bars(self, symbol: str, timeframe: str, lookback: int) -> List[Bar]:
        raise NotImplementedError

    @abstractmethod
    async def get_latest_price(self, symbol: str) -> float:
        raise NotImplementedError

    @abstractmethod
    async def is_market_open(self, symbol: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def get_session_label(self, symbol: str) -> str:
        raise NotImplementedError
```

Implementation note: build one concrete provider first, but preserve this abstraction.

---

## 27.7 Required service classes

### ConfigLoader
Responsibilities:
- load YAML
- validate against `AppConfig`
- expose typed config

### IndicatorService
Responsibilities:
- compute EMA, RSI, MACD histogram, ATR, VWAP
- return consistent numeric outputs
- avoid partial NaN leakage into later layers

### StructureService
Responsibilities:
- detect recent swing highs and lows
- identify pullback anchors
- calculate session high and low
- assess breakout or rejection structure

### SnapshotBuilder
Responsibilities:
- combine bars, indicators, and structure metrics into a `MarketSnapshot`

### RegimeScorer
Responsibilities:
- produce deterministic normalized scores
- output state labels such as bullish, bearish, choppy, extended

### PromptBuilder
Responsibilities:
- transform `MarketSnapshot` into compact structured LLM input
- limit token bloat
- include only relevant fields

### LLMClient
Responsibilities:
- send prompt to provider
- enforce timeout and retries
- return raw response text

### DecisionParser
Responsibilities:
- parse JSON
- validate against `LLMDecision`
- fail closed to NO_TRADE on parse errors

### StopEngine
Responsibilities:
- compute stop using structure and ATR floors
- reject invalid or oversized stops

### TargetEngine
Responsibilities:
- compute TP1 and TP2 using deterministic rules
- compute reward-to-risk

### GateOrchestrator
Responsibilities:
- run all gates in order
- build `GateReport`

### SignalBuilder
Responsibilities:
- combine validated decision plus deterministic risk plan into `TradePlan`

### EventLogger
Responsibilities:
- persist full event lifecycle to JSONL and summary CSV

### ConsoleRenderer
Responsibilities:
- print operator-friendly NO_TRADE or SIGNAL output

---

## 27.8 Gate definitions with pass/fail contract

Every gate must implement:

```python
class BaseGate(ABC):
    @abstractmethod
    def evaluate(self, context) -> GateResult:
        raise NotImplementedError
```

### Required gates in V1

#### SessionGate
Pass if current local time is inside configured session windows.

#### FreshnessGate
Pass if latest bar timestamp is recent enough for each timeframe.

#### DataCompletenessGate
Pass if enough bars exist to compute indicators.

#### DirectionPermissionGate
Pass if decision direction is allowed by config.

#### SetupTypeGate
Pass if returned setup type is in allowed config list.

#### ConfidenceGate
Pass if confidence >= configured threshold.

#### ExtensionGate
Pass if entry is not too extended relative to VWAP, ATR, and structure.

#### StopSanityGate
Pass if stop distance is between min and max thresholds.

#### RewardRiskGate
Pass if computed R:R >= minimum threshold.

#### CooldownGate
Pass if cooldown window is not active.

#### DailySignalCapGate
Pass if daily signal count is below cap.

#### ParseIntegrityGate
Pass only if LLM response parsed and validated cleanly.

---

## 27.9 Deterministic stop logic specification

### LONG trade
1. Identify nearest valid structural low from recent 1-minute or 5-minute context.
2. Compute structural stop candidate = structural low minus structure buffer.
3. Compute ATR floor stop candidate = entry mid minus ATR × atr_stop_floor_mult.
4. Select the farther valid stop if needed to avoid being too tight.
5. Reject if final stop distance < min_stop_points.
6. Reject if final stop distance > max_stop_points.

### SHORT trade
1. Identify nearest valid structural high.
2. Compute structural stop candidate = structural high plus structure buffer.
3. Compute ATR floor stop candidate = entry mid plus ATR × atr_stop_floor_mult.
4. Select the farther valid stop if needed.
5. Reject if out of allowed bounds.

### Additional stop rules
- stop must not sit inside obvious noise range if structure is weak
- if no valid structural anchor exists, reject the trade
- do not allow the LLM to override final stop placement

---

## 27.10 Deterministic target logic specification

### TP1
Use the greater of:
- minimum configured reward multiple, or
- nearest meaningful structure target if farther and still realistic

### TP2
Optional second target can be:
- prior session extreme
- measured move extension
- second R-multiple target

### Example formulas

For LONG:
- risk = entry_mid - stop_loss
- TP1 = entry_mid + max(risk × min_reward_risk, atr_1m × atr_target_mult)
- TP2 = entry_mid + risk × 2.5

For SHORT:
- risk = stop_loss - entry_mid
- TP1 = entry_mid - max(risk × min_reward_risk, atr_1m × atr_target_mult)
- TP2 = entry_mid - risk × 2.5

Implementation note: all target formulas should be bounded by obvious structure when appropriate.

---

## 27.11 LLM prompt contract

### System prompt
The coding assistant should place a stable system prompt in code or config. Example:

```text
You are a disciplined futures trade adjudication engine.
You are given a structured market snapshot generated by deterministic analytics.
You must decide one of: LONG, SHORT, NO_TRADE.
Be selective.
Do not invent data.
Prefer NO_TRADE when setup quality is mixed, extended, conflicting, or late.
Return valid JSON only.
```

### User prompt template

```text
Evaluate the following market snapshot for a possible manual MNQ trade plan.

Symbol: {symbol}
Time: {as_of}
Last price: {last_price}
Session: {session}

1m indicators:
- EMA9: {ema_9_1m}
- EMA21: {ema_21_1m}
- EMA50: {ema_50_1m}
- VWAP: {vwap_1m}
- RSI: {rsi_1m}
- MACD histogram: {macd_hist_1m}
- ATR: {atr_1m}

5m indicators:
- EMA9: {ema_9_5m}
- EMA21: {ema_21_5m}
- EMA50: {ema_50_5m}
- RSI: {rsi_5m}
- ATR: {atr_5m}

Structure:
- Session high: {session_high}
- Session low: {session_low}
- Prior swing high: {prior_swing_high}
- Prior swing low: {prior_swing_low}
- Distance from VWAP: {distance_from_vwap}
- Volume spike ratio: {volume_spike_ratio}

Scores:
- Trend: {trend_score}
- Momentum: {momentum_score}
- Volatility: {volatility_score}
- Extension risk: {extension_risk}
- Structure quality: {structure_quality}
- Pullback quality: {pullback_quality}
- Breakout quality: {breakout_quality}
- Mean reversion risk: {mean_reversion_risk}
- Session alignment: {session_alignment}

State labels:
- Short trend: {short_trend_state}
- Medium trend: {medium_trend_state}
- Momentum state: {momentum_state}
- Volatility regime: {volatility_regime}

Return JSON with fields:
decision, confidence, setup_type, thesis, entry_style, entry_zone,
invalidation_hint, hold_minutes, do_not_trade_if.
```

### Parsing requirement
If the model returns anything other than valid schema-conforming JSON, the app must treat it as NO_TRADE.

---

## 27.12 Console output spec

### Signal renderer format
```text
============================================================
SIGNAL: LONG
Symbol: MNQ
Time: 2026-04-14 10:12:00 ET
Setup: pullback_continuation
Confidence: 72

ENTRY ZONE: 19502.00 - 19506.00
STOP LOSS: 19486.00
TAKE PROFIT 1: 19530.00
TAKE PROFIT 2: 19546.00
RISK:REWARD: 1.95
MAX HOLD: 20 min

THESIS:
Bullish continuation above VWAP after constructive pullback into EMA support.

DO NOT TRADE IF:
- Price runs above 19510 before entry
- Next 1m candle closes below VWAP
- Spread or liquidity looks abnormal

INSTRUCTIONS:
1. Enter only within the entry zone.
2. Immediately place stop loss at 19486.00.
3. Place target at 19530.00 or manage according to your plan.
4. Skip if invalidation occurs before entry.
============================================================
```

### No-trade renderer format
```text
NO TRADE
Reason summary:
- extension gate failed
- reward:risk insufficient
- confidence below threshold
```

---

## 27.13 Local storage schema

### JSONL event record
One JSON object per loop cycle.

Required keys:
- `event_time`
- `symbol`
- `snapshot`
- `pre_gate_report`
- `llm_decision_raw`
- `llm_decision_parsed`
- `post_gate_report`
- `trade_plan`
- `final_outcome`
- `final_reason`

### CSV signal summary columns
- timestamp
- symbol
- outcome
- bias
- setup_type
- confidence
- entry_min
- entry_max
- stop_loss
- take_profit_1
- reward_risk_ratio
- max_hold_minutes
- final_reason

---

## 27.14 CLI command specification

Use `typer`.

### Commands

#### `run`
Starts live loop.

Arguments:
- `--config path/to/settings.yaml`
- `--once` optional single-cycle mode
- `--debug-llm`

#### `replay`
Runs historical replay.

Arguments:
- `--input path/to/bars.csv`
- `--config path/to/settings.yaml`

#### `validate-config`
Loads and validates config, then exits.

#### `show-last-signal`
Prints the last recorded signal from storage.

#### `kill-switch on|off`
Toggles signal emission state.

---

## 27.15 Suggested `app.py` orchestration skeleton

```python
async def run_cycle(container) -> SignalEvent:
    config = container.config
    provider = container.provider

    bars_1m = await provider.get_recent_bars(config.instrument.symbol, "1m", config.lookbacks.bars_1m)
    bars_5m = await provider.get_recent_bars(config.instrument.symbol, "5m", config.lookbacks.bars_5m)
    bars_1h = await provider.get_recent_bars(config.instrument.symbol, "1h", config.lookbacks.bars_1h)
    last_price = await provider.get_latest_price(config.instrument.symbol)
    session_label = await provider.get_session_label(config.instrument.symbol)

    snapshot = container.snapshot_builder.build(
        symbol=config.instrument.symbol,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_1h=bars_1h,
        last_price=last_price,
        session_label=session_label,
    )

    pre_report = container.pre_gate_orchestrator.evaluate(snapshot)
    if not pre_report.all_passed:
        event = container.event_factory.no_trade(snapshot, pre_report, reason="pre-gates-failed")
        container.logger.log_event(event)
        container.renderer.render_no_trade(event)
        return event

    raw_response = await container.llm_client.evaluate(snapshot)
    parsed_decision = container.decision_parser.parse(raw_response)

    plan = container.signal_builder.build(snapshot, parsed_decision)
    post_report = container.post_gate_orchestrator.evaluate({
        "snapshot": snapshot,
        "decision": parsed_decision,
        "plan": plan,
    })

    if not post_report.all_passed or plan is None:
        event = container.event_factory.no_trade(
            snapshot,
            pre_report,
            post_report,
            raw_response=raw_response,
            parsed_decision=parsed_decision,
            reason="post-gates-failed",
        )
        container.logger.log_event(event)
        container.renderer.render_no_trade(event)
        return event

    event = container.event_factory.signal(
        snapshot=snapshot,
        pre_report=pre_report,
        post_report=post_report,
        raw_response=raw_response,
        parsed_decision=parsed_decision,
        trade_plan=plan,
    )
    container.logger.log_event(event)
    container.renderer.render_signal(plan)
    return event
```

---

## 27.16 Replay file format specification

Historical replay CSV should include at minimum:

```text
timestamp,open,high,low,close,volume,timeframe,symbol
```

If separate files are used per timeframe, standardize naming:
- `MNQ_1m.csv`
- `MNQ_5m.csv`
- `MNQ_1h.csv`

Replay mode must reproduce the exact same decision path as live mode except for provider access.

---

## 27.17 Test plan the AI coder must implement

### Unit tests
- indicator calculations
- structure detection
- stop engine
- target engine
- config validation
- schema parsing
- gate pass/fail behavior

### Integration tests
- full cycle with mocked provider and mocked LLM
- malformed JSON response handling
- stale data NO_TRADE behavior
- daily cap behavior
- cooldown behavior

### Replay tests
- load historical bars
- run cycle by cycle
- produce summary report

### Regression tests
- fixed snapshot input should produce stable parsed outputs and gate decisions

---

## 27.18 Minimal build acceptance criteria

The first code drop is acceptable only if it can:

1. run locally on macOS from terminal
2. load config successfully
3. fetch or mock bars through a provider interface
4. compute market snapshot fields
5. call an LLM or mocked LLM and parse strict JSON
6. compute deterministic stop and target
7. run all gates
8. emit either a clean SIGNAL or NO TRADE
9. write JSONL and CSV logs
10. pass baseline tests

---

## 27.19 AI coding handoff prompt

Use this exact handoff prompt with a coding model:

```text
Build a production-structured local Python application for macOS called mnq_signal_bot.

Requirements:
- Use Python 3.11+
- Use typer for CLI
- Use pydantic for models and config validation
- Use YAML config files
- Implement a provider abstraction for market data
- Implement deterministic feature engineering for EMA, VWAP, RSI, MACD histogram, ATR, session highs/lows, and structure anchors
- Implement a MarketSnapshot model
- Implement deterministic regime and setup scoring
- Implement an LLM client behind an interface
- LLM must return strict JSON parsed into an LLMDecision model
- Build deterministic stop-loss and target logic
- Build pre- and post-decision gating
- Output manual trade instructions to terminal
- Log all events to JSONL and signal summaries to CSV
- Include replay mode for historical bar CSVs
- Include pytest tests for the critical modules
- Organize the project exactly according to the documented folder structure unless a minor improvement is clearly justified
- Fail closed to NO_TRADE on parse errors, missing data, stale data, or invalid configuration

Do not build broker execution.
Do not build Robinhood integration.
Do not build auto-trading.
Focus on clarity, safety, typed models, and testability.
Start by scaffolding the project and implementing the deterministic core before the real LLM integration.
```

---

## 27.20 Recommended next implementation step

After generating the codebase scaffold, the next human review should focus on:

- whether the stop logic is sane
- whether the target logic is sane
- whether the gates are too permissive
- whether the LLM prompt is too verbose or too vague
- whether operator instructions are actually easy to follow manually

Those reviews matter more than cosmetic code polish.

