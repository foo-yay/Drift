from __future__ import annotations

from drift.models import GateReport, LLMDecision, MarketSnapshot


class MockLLMClient:
    """Drop-in replacement for LLMClient used in dry-run mode.

    Returns a canned LONG decision anchored to the snapshot's actual last_price
    so the stop/R:R math always produces a valid plan regardless of price level.
    """

    def adjudicate(
        self,
        snapshot: MarketSnapshot,
        gate_report: GateReport,
    ) -> tuple[LLMDecision, dict, str]:
        price = snapshot.last_price
        # Entry zone: 10–20 pts below current price (pullback entry)
        entry_high = round(price - 8.0, 2)
        entry_low = round(price - 18.0, 2)

        decision = LLMDecision(
            decision="LONG",
            confidence=74,
            setup_type="pullback_continuation",
            thesis=(
                "Bullish trend intact on both short and medium timeframes with price above VWAP. "
                "Momentum remains constructive. Pullback into the EMA cluster offers a defined-risk "
                "continuation entry with clear structural invalidation below the prior swing low."
            ),
            entry_style="buy_pullback",
            entry_zone=[entry_low, entry_high],
            invalidation_hint=f"1m close below {round(entry_low - 5.0, 2)} (prior pullback low and VWAP)",
            hold_minutes=20,
            do_not_trade_if=[
                f"price extends above {round(entry_high + 4.0, 2)} before entry — do not chase",
                "next 1m candle closes below VWAP prior to entry",
                "spread widens materially at time of entry",
            ],
        )
        return decision, decision.model_dump(), "[mock response — no API call made]"
