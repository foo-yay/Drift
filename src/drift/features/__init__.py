"""Feature engineering layer for Drift.

All indicators are implemented with plain pandas (no external indicator library required).
This keeps the math auditable, avoids C-extension build issues, and makes the codebase
self-contained.

Public API:
    FeatureEngine  — primary entry point; construct once, call compute() each cycle
    TrendFeatures  — EMA-based trend state
    MomentumFeatures — RSI, MACD
    VolatilityFeatures — ATR, regime
    VolumeFeatures — session VWAP, volume spikes
    StructureFeatures — rolling highs/lows, candle characteristics
"""

from drift.features.engine import FeatureEngine
from drift.features.momentum import MomentumFeatures
from drift.features.structure import StructureFeatures
from drift.features.trend import TrendFeatures
from drift.features.volatility import VolatilityFeatures
from drift.features.volume import VolumeFeatures

__all__ = [
    "FeatureEngine",
    "TrendFeatures",
    "MomentumFeatures",
    "VolatilityFeatures",
    "VolumeFeatures",
    "StructureFeatures",
]
