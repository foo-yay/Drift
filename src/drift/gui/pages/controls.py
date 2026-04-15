"""Controls — Page 4.  Built in Phase 10d."""
from __future__ import annotations

import streamlit as st


def page() -> None:
    st.title("⚙️ Controls")
    st.info(
        "**Coming in Phase 10d.**\n\n"
        "Engine start/stop toggle, kill switch, sandbox mode toggle, and "
        "run-one-cycle-now button.",
        icon="🔜",
    )
