from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from drift.models import TradePlan

logger = logging.getLogger(__name__)

_SOUND_FILE = Path(__file__).resolve().parents[3] / "assets" / "sounds" / "cash_register.wav"


def notify_signal(plan: TradePlan, *, approval_required: bool = False) -> None:
    """Send a macOS desktop notification when a trade signal fires.

    Uses `osascript` — no external dependencies required on macOS.
    Silently skips on non-macOS platforms or if osascript is unavailable.
    """
    direction_emoji = "🟢" if plan.bias == "LONG" else "🔴"
    action = "⏳ APPROVE in GUI" if approval_required else ""
    title = f"{direction_emoji} Drift Signal — {plan.symbol} {plan.bias}"
    body = (
        f"Entry: {plan.entry_min:.2f}–{plan.entry_max:.2f}  "
        f"Stop: {plan.stop_loss:.2f}  "
        f"TP1: {plan.take_profit_1:.2f}  "
        f"Conf: {plan.confidence}%"
        + (f"  {action}" if action else "")
    )
    _send(title, body)
    _play_sound()


def notify_blocked(gate_name: str, reason: str, symbol: str) -> None:
    """Optional notification when a high-scoring setup is gate-blocked (not used by default)."""
    _send(f"⛔ Drift Blocked — {symbol}", f"{gate_name}: {reason}")


def _send(title: str, body: str) -> None:
    """Fire an osascript notification. Swallows all errors."""
    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}" '
        f'sound name "Ping"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        logger.debug("osascript not available — desktop notifications skipped.")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Desktop notification failed: %s", exc)


def _play_sound() -> None:
    """Play the cash-register sound via macOS afplay. Non-blocking."""
    if not _SOUND_FILE.exists():
        logger.debug("Sound file not found: %s", _SOUND_FILE)
        return
    try:
        subprocess.Popen(  # noqa: S603
            ["afplay", str(_SOUND_FILE)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.debug("afplay not available — sound playback skipped.")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Sound playback failed: %s", exc)


def _esc(text: str) -> str:
    """Escape double quotes and backslashes for AppleScript string literals."""
    return text.replace("\\", "\\\\").replace('"', '\\"')
