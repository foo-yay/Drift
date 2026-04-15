"""Settings — Page 5.  Built in Phase 10d."""
from __future__ import annotations

import streamlit as st


def page() -> None:
    st.title("🔧 Settings")
    st.info(
        "**Coming in Phase 10d.**\n\n"
        "Form-based config editor for instrument, session, gates, risk, and LLM settings.  "
        "Pydantic-validated before writing — no YAML editing required.",
        icon="🔜",
    )
