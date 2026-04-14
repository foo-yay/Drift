from __future__ import annotations

from drift.config.models import GatesSection
from drift.gates.base import Gate
from drift.models import GateResult, MarketSnapshot


class RegimeGate(Gate):
    """Blocks signals when the market regime offers no clear directional edge.

    Rules (all evaluated; first failure wins):
        1. Gate disabled → always passes
        2. trend_score < min_trend_score → blocked (no trend conviction)
        3. momentum_score < min_momentum_score → blocked (no momentum)
        4. block_on_extreme_volatility and volatility_regime == "extreme"
           → blocked (erratic / untradeble conditions)

    Thresholds are intentionally conservative — a no-trade is safer than a
    forced trade in a choppy or directionless regime.
    """

    @property
    def name(self) -> str:
        return "regime"

    def __init__(self, config: GatesSection) -> None:
        self._config = config

    def evaluate(self, snapshot: MarketSnapshot) -> GateResult:
        if not self._config.regime_enabled:
            return GateResult(
                gate_name=self.name,
                passed=True,
                reason="Regime gate disabled in config.",
            )

        if snapshot.trend_score < self._config.min_trend_score:
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=(
                    f"Trend score too low for a clear edge "
                    f"({snapshot.trend_score} < min {self._config.min_trend_score})."
                ),
            )

        if snapshot.momentum_score < self._config.min_momentum_score:
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason=(
                    f"Momentum score too low for a clear edge "
                    f"({snapshot.momentum_score} < min {self._config.min_momentum_score})."
                ),
            )

        if self._config.block_on_extreme_volatility and snapshot.volatility_regime == "extreme":
            return GateResult(
                gate_name=self.name,
                passed=False,
                reason="Volatility regime is extreme — risk of erratic, untradeble price action.",
            )

        return GateResult(
            gate_name=self.name,
            passed=True,
            reason=(
                f"Regime acceptable — trend {snapshot.trend_score}, "
                f"momentum {snapshot.momentum_score}, "
                f"volatility '{snapshot.volatility_regime}'."
            ),
        )
