"""Signal History — Page 2.  Built in Phase 10c."""
from __future__ import annotations

import streamlit as st


def page() -> None:
    st.title("📋 Signal History")
    st.info(
        "**Coming in Phase 10c.**\n\n"
        "This page will show a full auditable ledger of every signal ever generated, "
        "with date/source/outcome filters, win rate metrics, and click-to-detail for each row.",
        icon="🔜",
    )
