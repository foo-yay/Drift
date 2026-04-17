from __future__ import annotations

import logging
import subprocess

from drift.models import TradePlan

logger = logging.getLogger(__name__)


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


def _esc(text: str) -> str:
    """Escape double quotes and backslashes for AppleScript string literals."""
    return text.replace("\\", "\\\\").replace('"', '\\"')
