from __future__ import annotations

from drift.models import GateReport, LLMDecision, MarketSnapshot

_CANNED_DECISION = LLMDecision(
    decision="LONG",
    confidence=74,
    setup_type="pullback_continuation",
    thesis=(
        "Bullish trend intact on both short and medium timeframes with price above VWAP. "
        "Momentum remains constructive. Pullback into the EMA cluster offers a defined-risk "
        "continuation entry with clear structural invalidation below the prior swing low."
    ),
    entry_style="buy_pullback",
    entry_zone=[25_960.0, 25_970.0],
    invalidation_hint="1m close below 25_945 (prior pullback low and VWAP)",
    hold_minutes=20,
    do_not_trade_if=[
        "price extends above 25_990 before entry — do not chase",
        "next 1m candle closes below VWAP prior to entry",
        "spread widens materially at time of entry",
    ],
)

_CANNED_RAW = _CANNED_DECISION.model_dump()
_CANNED_TEXT = "[mock response — no API call made]"


class MockLLMClient:
    """Drop-in replacement for LLMClient used in dry-run mode.

    Returns a fixed canned LONG decision so the full rendering pipeline
    (plan builder, console output, logging) can be exercised without
    spending API credits or requiring RTH session times.
    """

    def adjudicate(
        self,
        snapshot: MarketSnapshot,
        gate_report: GateReport,
    ) -> tuple[LLMDecision, dict, str]:
        return _CANNED_DECISION, _CANNED_RAW, _CANNED_TEXT
