"""Liquidity Sweep + FVG + Pin Bar setup scanner.

Pipeline (same for both directions, mirrored)
----------------------------------------------
SHORT:
  1. Bearish liquidity sweep detected (recent swing high swept, closed below)
  2. Bearish FVG created after the sweep  OR  price revisits a bearish FVG
  3. Confirmation: bearish pin bar  OR  fresh bearish momentum FVG
  4. Entry: close of confirmation candle (entry zone = last bar's OHLC range)
  5. Stop: above the rejection high + stop_buffer
  6. Target: nearest swing low below entry, then session low

LONG: symmetric.

Decision rules
--------------
- EITHER trigger (sweep) is required — there is no divergence fallback in v1.
- EITHER structural follow-through (new FVG or existing FVG interaction) is
  required.
- EITHER confirmation type (pin bar or fresh directional FVG) is required.
- If none of the required gates pass → NO_TRADE with debug dict explaining why.
- If R:R < min_reward_risk → NO_TRADE (same as the existing LLM path).
- All thresholds come from LiquiditySweepConfig (see config/models.py).

Confidence scoring (0–100, deterministic)
------------------------------------------
Base: 40 (sweep alone)
  + 20 if structural FVG follow-through confirmed
  + 20 if confirmation type is pin bar (slightly higher conviction than momentum FVG)
  + 15 if confirmation type is momentum FVG only
  +  5 if sweep penetration > 2× min_sweep_distance (strong sweep)
  -  5 if FVG gap is small (< 2× min_fvg_size)
Clamp to [40, 100].

Note: confidence 40 by itself would be filtered by risk.min_confidence = 60,
so a clean 3-stage confirmation is required to clear the threshold.
"""
from __future__ import annotations

import logging

from drift.config.models import AppConfig
from drift.models import Bar
from drift.strategy.primitives.fvg import FVG, find_fvgs_after
from drift.strategy.primitives.pinbar import find_pin_bars_after
from drift.strategy.primitives.sweeps import SweepResult, detect_bearish_sweep, detect_bullish_sweep
from drift.strategy.primitives.targets import find_long_targets, find_short_targets
from drift.strategy.result import SetupResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scan(bars_5m: list[Bar], config: AppConfig) -> SetupResult:
    """Scan 5-minute bars for a liquidity sweep setup.

    Args:
        bars_5m: 5-minute bars, oldest-first.  The scanner operates entirely
                 on this timeframe.
        config:  Full AppConfig — uses config.liquidity_sweep for thresholds.

    Returns:
        SetupResult with decision LONG / SHORT / NO_TRADE.
    """
    cfg = config.liquidity_sweep
    debug: dict[str, object] = {
        "sweep_detected": False,
        "sweep_kind": None,
        "sweep_level": None,
        "fvg_detected": False,
        "fvg_kind": None,
        "pin_bar_detected": False,
        "momentum_fvg_detected": False,
        "target_liquidity_found": False,
        "no_trade_reason": "",
    }

    if len(bars_5m) < cfg.min_bars_required:
        debug["no_trade_reason"] = f"insufficient bars: {len(bars_5m)} < {cfg.min_bars_required}"
        return _no_trade(debug)

    # -----------------------------------------------------------------------
    # SHORT path
    # -----------------------------------------------------------------------
    short_result = _try_short(bars_5m, cfg, debug.copy())
    if short_result.decision == "SHORT":
        return short_result

    # -----------------------------------------------------------------------
    # LONG path
    # -----------------------------------------------------------------------
    long_result = _try_long(bars_5m, cfg, debug.copy())
    if long_result.decision == "LONG":
        return long_result

    # Neither direction has a valid setup — return the most informative debug
    # (prefer the direction that got furthest through the pipeline)
    combined_debug = _merge_debug(short_result.debug, long_result.debug)
    combined_debug["no_trade_reason"] = "no valid sweep setup in either direction"
    return _no_trade(combined_debug)


# ---------------------------------------------------------------------------
# Direction-specific scanners
# ---------------------------------------------------------------------------

def _try_short(bars: list[Bar], cfg, base_debug: dict) -> SetupResult:
    """Attempt to build a SHORT setup.  Returns NO_TRADE if any required gate fails."""
    debug = base_debug

    # --- Gate 1: bearish sweep ---
    sweep = detect_bearish_sweep(
        bars,
        swing_lookback=cfg.swing_lookback,
        min_sweep_distance=cfg.min_sweep_distance,
        max_close_bars=cfg.max_rejection_close_bars,
        max_swing_age_bars=cfg.max_swing_age_bars,
    )
    if sweep is None:
        debug["no_trade_reason"] = "short: no bearish sweep found"
        return _no_trade(debug)

    debug["sweep_detected"] = True
    debug["sweep_kind"] = "bearish"
    debug["sweep_level"] = sweep.level

    # Sweep must be recent (within max_bars_from_sweep of the last bar)
    bars_since_sweep = (len(bars) - 1) - sweep.rejection_bar_index
    if bars_since_sweep > cfg.max_bars_from_sweep:
        debug["no_trade_reason"] = (
            f"short: sweep is stale ({bars_since_sweep} bars ago, max {cfg.max_bars_from_sweep})"
        )
        return _no_trade(debug)

    # --- Gate 2: bearish FVG structural follow-through ---
    fvgs_after_sweep = find_fvgs_after(
        bars,
        after_bar_index=sweep.rejection_bar_index,
        kind="bearish",
        min_gap_size=cfg.min_fvg_size,
    )
    if not fvgs_after_sweep:
        debug["no_trade_reason"] = "short: no bearish FVG found after sweep"
        return _no_trade(debug)

    fvg = fvgs_after_sweep[-1]  # most recent bearish FVG
    debug["fvg_detected"] = True
    debug["fvg_kind"] = "bearish"

    # --- Gate 3: confirmation (pin bar OR fresh bearish momentum FVG) ---
    confirmation_bar_index = fvg.displacement_bar_index  # look for confirmation after FVG forms
    pin_bars = find_pin_bars_after(
        bars,
        after_bar_index=confirmation_bar_index,
        kind="bearish",
        min_wick_ratio=cfg.pin_bar_min_wick_ratio,
        max_body_ratio=cfg.pin_bar_max_body_ratio,
        close_zone_ratio=cfg.pin_bar_close_zone_ratio,
    )
    momentum_fvgs = find_fvgs_after(
        bars,
        after_bar_index=fvg.displacement_bar_index + 1,
        kind="bearish",
        min_gap_size=cfg.min_fvg_size,
    )

    if not pin_bars and not momentum_fvgs:
        debug["no_trade_reason"] = "short: no confirmation (bearish pin bar or momentum FVG)"
        return _no_trade(debug)

    # Prefer pin bar over momentum FVG when both exist
    if pin_bars:
        debug["pin_bar_detected"] = True
        confirmation_type = "pin_bar"
        confirmation_bar = bars[pin_bars[-1].bar_index]
        confirmation_bar_idx = pin_bars[-1].bar_index
    else:
        debug["momentum_fvg_detected"] = True
        confirmation_type = "momentum_fvg"
        mom_fvg = momentum_fvgs[-1]
        confirmation_bar = bars[mom_fvg.displacement_bar_index]
        confirmation_bar_idx = mom_fvg.displacement_bar_index

    # --- Entry zone ---
    # Entry = within the body of the confirmation candle (market-on-close style)
    entry_min = min(confirmation_bar.open, confirmation_bar.close)
    entry_max = max(confirmation_bar.open, confirmation_bar.close)

    # --- Stop: above the sweep high + buffer ---
    rejection_high = bars[sweep.sweep_bar_index].high
    stop_loss = round(rejection_high + cfg.stop_buffer, 4)
    stop_distance = stop_loss - entry_min  # worst case: stop is above entry_min

    if stop_distance <= 0:
        debug["no_trade_reason"] = "short: stop would be at or below entry"
        return _no_trade(debug)

    # --- Targets ---
    targets = find_short_targets(
        bars,
        entry_min=entry_min,
        min_target_distance=cfg.min_target_distance,
        swing_lookback=cfg.swing_lookback,
        max_swing_age_bars=cfg.max_swing_age_bars,
        n_targets=2,
    )
    if not targets:
        debug["no_trade_reason"] = "short: no target liquidity found below entry"
        return _no_trade(debug)

    debug["target_liquidity_found"] = True
    tp1 = targets[0].price
    tp2 = targets[1].price if len(targets) > 1 else None

    # R:R check (use TP1 vs worst-case entry fill)
    profit_distance = entry_min - tp1
    rr = round(profit_distance / stop_distance, 2) if stop_distance > 0 else 0.0

    if rr < cfg.min_reward_risk:
        debug["no_trade_reason"] = f"short: R:R {rr:.2f} < min {cfg.min_reward_risk}"
        return _no_trade(debug)

    confidence = _score_confidence(sweep, fvg, confirmation_type, cfg)

    invalidation = [
        f"Price closes back above rejection high {rejection_high:.2f}",
        f"No fill within entry zone {entry_min:.2f}–{entry_max:.2f}",
        f"News / session gate fires",
    ]

    thesis = (
        f"Bearish liquidity sweep of {sweep.level:.2f} (+{sweep.penetration:.2f} pts), "
        f"bearish FVG {fvg.bottom:.2f}–{fvg.top:.2f} formed, "
        f"confirmed by {confirmation_type.replace('_', ' ')}. "
        f"Target: {tp1:.2f} (nearest swing low). R:R {rr:.2f}."
    )

    log.info(
        "SHORT setup: sweep=%.2f, FVG=%s–%s, conf=%s, SL=%.2f, TP1=%.2f, RR=%.2f",
        sweep.level, fvg.bottom, fvg.top, confirmation_type, stop_loss, tp1, rr,
    )

    return SetupResult(
        decision="SHORT",
        setup_type="liquidity_sweep",
        confidence=confidence,
        context_trigger=f"bearish_sweep_of_{sweep.level:.2f}",
        confirmation_type=confirmation_type,
        entry_min=round(entry_min, 4),
        entry_max=round(entry_max, 4),
        stop_loss=stop_loss,
        take_profit_1=round(tp1, 4),
        take_profit_2=round(tp2, 4) if tp2 is not None else None,
        reward_risk_ratio=rr,
        thesis=thesis,
        invalidation_conditions=invalidation,
        debug=debug,
    )


def _try_long(bars: list[Bar], cfg, base_debug: dict) -> SetupResult:
    """Attempt to build a LONG setup.  Returns NO_TRADE if any required gate fails."""
    debug = base_debug

    # --- Gate 1: bullish sweep ---
    sweep = detect_bullish_sweep(
        bars,
        swing_lookback=cfg.swing_lookback,
        min_sweep_distance=cfg.min_sweep_distance,
        max_close_bars=cfg.max_rejection_close_bars,
        max_swing_age_bars=cfg.max_swing_age_bars,
    )
    if sweep is None:
        debug["no_trade_reason"] = "long: no bullish sweep found"
        return _no_trade(debug)

    debug["sweep_detected"] = True
    debug["sweep_kind"] = "bullish"
    debug["sweep_level"] = sweep.level

    bars_since_sweep = (len(bars) - 1) - sweep.rejection_bar_index
    if bars_since_sweep > cfg.max_bars_from_sweep:
        debug["no_trade_reason"] = (
            f"long: sweep is stale ({bars_since_sweep} bars ago, max {cfg.max_bars_from_sweep})"
        )
        return _no_trade(debug)

    # --- Gate 2: bullish FVG structural follow-through ---
    fvgs_after_sweep = find_fvgs_after(
        bars,
        after_bar_index=sweep.rejection_bar_index,
        kind="bullish",
        min_gap_size=cfg.min_fvg_size,
    )
    if not fvgs_after_sweep:
        debug["no_trade_reason"] = "long: no bullish FVG found after sweep"
        return _no_trade(debug)

    fvg = fvgs_after_sweep[-1]
    debug["fvg_detected"] = True
    debug["fvg_kind"] = "bullish"

    # --- Gate 3: confirmation ---
    confirmation_bar_index = fvg.displacement_bar_index
    pin_bars = find_pin_bars_after(
        bars,
        after_bar_index=confirmation_bar_index,
        kind="bullish",
        min_wick_ratio=cfg.pin_bar_min_wick_ratio,
        max_body_ratio=cfg.pin_bar_max_body_ratio,
        close_zone_ratio=cfg.pin_bar_close_zone_ratio,
    )
    momentum_fvgs = find_fvgs_after(
        bars,
        after_bar_index=fvg.displacement_bar_index + 1,
        kind="bullish",
        min_gap_size=cfg.min_fvg_size,
    )

    if not pin_bars and not momentum_fvgs:
        debug["no_trade_reason"] = "long: no confirmation (bullish pin bar or momentum FVG)"
        return _no_trade(debug)

    if pin_bars:
        debug["pin_bar_detected"] = True
        confirmation_type = "pin_bar"
        confirmation_bar = bars[pin_bars[-1].bar_index]
        confirmation_bar_idx = pin_bars[-1].bar_index
    else:
        debug["momentum_fvg_detected"] = True
        confirmation_type = "momentum_fvg"
        mom_fvg = momentum_fvgs[-1]
        confirmation_bar = bars[mom_fvg.displacement_bar_index]
        confirmation_bar_idx = mom_fvg.displacement_bar_index

    # --- Entry zone ---
    entry_min = min(confirmation_bar.open, confirmation_bar.close)
    entry_max = max(confirmation_bar.open, confirmation_bar.close)

    # --- Stop: below the sweep low + buffer ---
    rejection_low = bars[sweep.sweep_bar_index].low
    stop_loss = round(rejection_low - cfg.stop_buffer, 4)
    stop_distance = entry_max - stop_loss

    if stop_distance <= 0:
        debug["no_trade_reason"] = "long: stop would be at or above entry"
        return _no_trade(debug)

    # --- Targets ---
    targets = find_long_targets(
        bars,
        entry_max=entry_max,
        min_target_distance=cfg.min_target_distance,
        swing_lookback=cfg.swing_lookback,
        max_swing_age_bars=cfg.max_swing_age_bars,
        n_targets=2,
    )
    if not targets:
        debug["no_trade_reason"] = "long: no target liquidity found above entry"
        return _no_trade(debug)

    debug["target_liquidity_found"] = True
    tp1 = targets[0].price
    tp2 = targets[1].price if len(targets) > 1 else None

    profit_distance = tp1 - entry_max
    rr = round(profit_distance / stop_distance, 2) if stop_distance > 0 else 0.0

    if rr < cfg.min_reward_risk:
        debug["no_trade_reason"] = f"long: R:R {rr:.2f} < min {cfg.min_reward_risk}"
        return _no_trade(debug)

    confidence = _score_confidence(sweep, fvg, confirmation_type, cfg)

    invalidation = [
        f"Price closes back below rejection low {rejection_low:.2f}",
        f"No fill within entry zone {entry_min:.2f}–{entry_max:.2f}",
        f"News / session gate fires",
    ]

    thesis = (
        f"Bullish liquidity sweep of {sweep.level:.2f} (-{sweep.penetration:.2f} pts), "
        f"bullish FVG {fvg.bottom:.2f}–{fvg.top:.2f} formed, "
        f"confirmed by {confirmation_type.replace('_', ' ')}. "
        f"Target: {tp1:.2f} (nearest swing high). R:R {rr:.2f}."
    )

    log.info(
        "LONG setup: sweep=%.2f, FVG=%s–%s, conf=%s, SL=%.2f, TP1=%.2f, RR=%.2f",
        sweep.level, fvg.bottom, fvg.top, confirmation_type, stop_loss, tp1, rr,
    )

    return SetupResult(
        decision="LONG",
        setup_type="liquidity_sweep",
        confidence=confidence,
        context_trigger=f"bullish_sweep_of_{sweep.level:.2f}",
        confirmation_type=confirmation_type,
        entry_min=round(entry_min, 4),
        entry_max=round(entry_max, 4),
        stop_loss=stop_loss,
        take_profit_1=round(tp1, 4),
        take_profit_2=round(tp2, 4) if tp2 is not None else None,
        reward_risk_ratio=rr,
        thesis=thesis,
        invalidation_conditions=invalidation,
        debug=debug,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_trade(debug: dict) -> SetupResult:
    return SetupResult(
        decision="NO_TRADE",
        setup_type="liquidity_sweep",
        no_trade_reason=str(debug.get("no_trade_reason", "unknown")),
        debug=debug,
    )


def _score_confidence(
    sweep: SweepResult,
    fvg: FVG,
    confirmation_type: str,
    cfg,
) -> int:
    score = 40  # base: sweep only

    # Structural FVG follow-through
    score += 20

    # Confirmation quality
    if confirmation_type == "pin_bar":
        score += 20
    else:
        score += 15  # momentum_fvg

    # Strong sweep bonus
    if sweep.penetration >= cfg.min_sweep_distance * 2:
        score += 5

    # Small FVG penalty
    if fvg.gap_size < cfg.min_fvg_size * 2:
        score -= 5

    return max(40, min(100, score))


def _merge_debug(d1: dict, d2: dict) -> dict:
    """Merge two debug dicts; prefer the one that got further (more True flags)."""
    score1 = sum(1 for v in d1.values() if v is True)
    score2 = sum(1 for v in d2.values() if v is True)
    return d1 if score1 >= score2 else d2
