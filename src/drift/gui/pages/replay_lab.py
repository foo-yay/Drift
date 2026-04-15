"""Replay Lab — Page 3.  Built in Phase 10c."""
from __future__ import annotations

import streamlit as st


def page() -> None:
    st.title("🔄 Replay Lab")
    st.info(
        "**Coming in Phase 10c.**\n\n"
        "Run historical replays by date range, view stored results without re-calling the LLM, "
        "and manage overwrite / deduplication of existing replay signals.",
        icon="🔜",
    )
