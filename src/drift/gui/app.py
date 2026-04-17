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

import streamlit as st

from drift.gui.pages import (
    controls,
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
    st.Page(controls.page,       title="Controls",       icon="⚙️", url_path="controls"),
    st.Page(settings.page,       title="Settings",       icon="🔧", url_path="settings"),
]

# ---------------------------------------------------------------------------
# Sidebar header
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("## 📈 Drift")
    st.caption("Local MNQ signal engine")
    st.divider()

nav = st.navigation(_pages, position="sidebar")
nav.run()
