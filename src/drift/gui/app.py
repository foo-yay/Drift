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
# Sidebar header + live price ticker
# ---------------------------------------------------------------------------

@st.fragment(run_every=5)
def _sidebar_price() -> None:
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from drift.gui.state import get_config, get_live_price

    config = get_config()
    symbol = config.instrument.symbol
    _ET = ZoneInfo("America/New_York")
    price = get_live_price(symbol)
    if price is not None:
        now_et = datetime.now(tz=_ET)
        st.markdown(
            f"<div style='margin:4px 0 2px 0;line-height:1.1'>"
            f"<span style='font-size:1.6rem;font-weight:700;color:#f5f5f5'>"
            f"{price:,.2f}</span>"
            f"<span style='color:#777;font-size:0.75rem;margin-left:8px'>"
            f"{symbol} · {now_et.strftime('%H:%M:%S')} ET</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("Price unavailable")


with st.sidebar:
    st.markdown("## 📈 Drift")
    _sidebar_price()
    st.divider()

nav = st.navigation(_pages, position="sidebar")

# ---------------------------------------------------------------------------
# Active position banner — visible on every page
# ---------------------------------------------------------------------------
render_position_banner()

nav.run()
