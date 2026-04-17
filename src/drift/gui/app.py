"""Drift GUI — Streamlit multi-page application entrypoint.

Launch via:
    drift gui                   # recommended
    streamlit run src/drift/gui/app.py  # direct

``st.set_page_config`` must be the very first Streamlit call in this file.
"""
from __future__ import annotations

from pathlib import Path

# Load .env before anything reads env vars (API keys, config path, etc.)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parents[3] / ".env", override=False)

import os
import yaml
import streamlit as st

from drift.gui.components.position_banner import render_position_banner
from drift.gui.pages import (
    controls,
    dev_tools,
    live_monitor,
    orders,
    replay_lab,
    settings,
    signal_history,
)
from drift.gui.scheduler import ensure_scheduler_running

st.set_page_config(
    page_title="Drift",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Background scheduler — starts once per process, runs regardless of browser
# ---------------------------------------------------------------------------
ensure_scheduler_running()

# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

_pages = [
    st.Page(live_monitor.page,   title="Live Monitor",   icon="📡", url_path="live",    default=True),
    st.Page(signal_history.page, title="Signal History", icon="📋", url_path="history"),
    st.Page(orders.page,         title="Orders",         icon="🏦", url_path="orders"),
    st.Page(replay_lab.page,     title="Replay Lab",     icon="🔄", url_path="replay"),
    st.Page(controls.page,       title="Controls",       icon="⚙️",  url_path="controls"),
    st.Page(dev_tools.page,      title="Dev Tools",      icon="🛠️",  url_path="dev"),
    st.Page(settings.page,       title="Settings",       icon="🔧",  url_path="settings"),
]

# ---------------------------------------------------------------------------
# Sidebar header
# ---------------------------------------------------------------------------

_MODES = ["paper-live", "sandbox", "dry-run", "replay", "llm-debug"]
_MODE_ICONS = {
    "paper-live": "🟢",
    "sandbox":    "🟡",
    "dry-run":    "⚪",
    "replay":     "🔵",
    "llm-debug":  "🟣",
}
_CFG_PATH = (
    Path(os.environ["DRIFT_CONFIG"])
    if os.environ.get("DRIFT_CONFIG") and Path(os.environ["DRIFT_CONFIG"]).exists()
    else Path(__file__).parents[3] / "config" / "settings.yaml"
)


def _set_mode(new_mode: str) -> None:
    """Persist the selected mode to settings.yaml and reload config cache."""
    with _CFG_PATH.open("r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}
    raw.setdefault("app", {})["mode"] = new_mode
    tmp = _CFG_PATH.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.dump(raw, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(_CFG_PATH)
    st.cache_resource.clear()


def _current_mode() -> str:
    with _CFG_PATH.open("r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}
    return raw.get("app", {}).get("mode", "sandbox")


with st.sidebar:
    st.markdown("## 📈 Drift")
    st.caption("Local MNQ signal engine")
    st.divider()

    _mode = _current_mode()
    _new_mode = st.selectbox(
        "Mode",
        _MODES,
        index=_MODES.index(_mode) if _mode in _MODES else 0,
        format_func=lambda m: f"{_MODE_ICONS[m]} {m}",
        key="sidebar_mode_select",
    )
    if _new_mode != _mode:
        _set_mode(_new_mode)
        st.rerun()

    st.divider()

nav = st.navigation(_pages, position="sidebar")

# ---------------------------------------------------------------------------
# Active position banner — visible on every page
# ---------------------------------------------------------------------------
render_position_banner()

nav.run()
