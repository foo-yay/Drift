"""Controls — engine toggle, kill switch, sandbox mode, run-now.

All state is expressed as filesystem artefacts so it is readable by both the
GUI and the CLI:

- Kill switch:  ``config.gates.kill_switch_path`` (create = active)
- Sandbox mode: ``data/.sandbox`` sentinel file (create = active)
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from drift.gui.state import get_config, get_store, project_root

_ROOT = project_root()


@st.cache_resource
def _load_config():
    return get_config()


@st.cache_resource
def _open_store(config):
    return get_store(config)


def page() -> None:
    st.title("⚙️ Controls")

    config = _load_config()

    kill_path    = _ROOT / config.gates.kill_switch_path
    sandbox_path = _ROOT / "data" / ".sandbox"

    # ------------------------------------------------------------------
    # Kill Switch
    # ------------------------------------------------------------------
    st.subheader("Kill Switch")
    st.caption(
        "When active, every pipeline cycle is blocked before any analysis runs. "
        "Toggle this to immediately halt or resume signal generation."
    )

    kill_active = kill_path.exists()

    if kill_active:
        st.error("🔴 KILL SWITCH ACTIVE — all signals blocked", icon="🚨")
        if st.button("✅ Resume signals", key="kill_resume", type="primary"):
            try:
                kill_path.unlink(missing_ok=True)
                st.success("Kill switch cleared — signals will resume on the next cycle.", icon="✅")
                st.rerun()
            except OSError as exc:
                st.error(f"Could not remove kill switch file: {exc}")
    else:
        st.success("🟢 Running — signals enabled", icon="✅")
        if st.button("🛑 Activate kill switch", key="kill_activate", type="secondary"):
            try:
                kill_path.parent.mkdir(parents=True, exist_ok=True)
                kill_path.touch()
                st.warning("Kill switch activated — signals blocked.", icon="🔴")
                st.rerun()
            except OSError as exc:
                st.error(f"Could not create kill switch file: {exc}")

    st.divider()

    # ------------------------------------------------------------------
    # Sandbox Mode
    # ------------------------------------------------------------------
    st.subheader("Sandbox Mode")
    st.caption(
        "Sandbox mode uses the mock LLM client (no API cost) and writes signals to a "
        "separate database. Useful for testing without affecting real signal history."
    )

    sandbox_active = sandbox_path.exists()

    col_sb, col_indicator = st.columns([3, 1])
    with col_indicator:
        if sandbox_active:
            st.markdown(
                "<p style='color:#f5a623; font-size:0.9rem; margin-top:8px'>● Sandbox ON</p>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<p style='color:#4caf50; font-size:0.9rem; margin-top:8px'>● Live mode</p>",
                unsafe_allow_html=True,
            )

    with col_sb:
        if sandbox_active:
            if st.button("Disable sandbox", key="sandbox_off", type="secondary"):
                sandbox_path.unlink(missing_ok=True)
                st.rerun()
        else:
            if st.button("Enable sandbox", key="sandbox_on", type="secondary"):
                sandbox_path.parent.mkdir(parents=True, exist_ok=True)
                sandbox_path.touch()
                st.rerun()

    st.divider()

    # ------------------------------------------------------------------
    # Run Now
    # ------------------------------------------------------------------
    st.subheader("Manual Cycle")
    st.caption("Trigger one analysis cycle immediately, outside the scheduler loop.")

    if st.button("▶ Run Now", key="controls_run_now", type="primary"):
        _run_cycle(config)

    # Show output if a cycle was just run from this page
    if st.session_state.get("_ctrl_show_output"):
        outcome = st.session_state.get("_ctrl_outcome", "")
        output  = st.session_state.get("_ctrl_output", "")
        error   = st.session_state.get("_ctrl_error", "")

        if outcome == "success":
            st.success("Cycle completed.", icon="✅")
        elif outcome == "error":
            st.error(f"Cycle failed: {error}", icon="🚨")

        if output:
            st.code(output, language=None)

        st.session_state.pop("_ctrl_show_output", None)

    st.divider()

    # ------------------------------------------------------------------
    # Scheduler status
    # ------------------------------------------------------------------
    st.subheader("Scheduler")

    try:
        from drift.gui.scheduler import ensure_scheduler_running
        scheduler = ensure_scheduler_running()
        alive = scheduler.is_alive()
        snap  = scheduler.state.snapshot()

        if alive:
            st.success("● Scheduler running", icon="✅")
        else:
            st.error("● Scheduler stopped — restart `drift gui` to recover", icon="🚨")

        c1, c2 = st.columns(2)
        last_run = snap["last_run_utc"]
        c1.metric(
            "Last scheduled cycle",
            last_run.strftime("%H:%M ET") if last_run else "—",
        )
        c2.metric("Last outcome", snap["last_outcome"] or "—")
        if snap["last_error"]:
            st.caption(f"Error: {snap['last_error']}")
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Scheduler status unavailable: {exc}")


def _run_cycle(config) -> None:
    """Run one cycle, capture output, store result in session state."""
    import io

    import streamlit as st
    from rich.console import Console

    import drift.output.console as console_mod
    from drift.app import DriftApplication
    from drift.utils.config import load_app_config

    root       = project_root()
    config_path = str(root / "config" / "settings.yaml")
    sandbox    = (root / "data" / ".sandbox").exists()

    abs_storage = config.storage.model_copy(update={
        "jsonl_event_log":         str(root / config.storage.jsonl_event_log),
        "sqlite_path":             str(root / config.storage.sqlite_path),
        "sandbox_jsonl_event_log": str(root / config.storage.sandbox_jsonl_event_log),
        "sandbox_sqlite_path":     str(root / config.storage.sandbox_sqlite_path),
    })
    abs_config = config.model_copy(update={"storage": abs_storage})

    buf = io.StringIO()
    capture = Console(file=buf, force_terminal=False, no_color=True, width=100)
    orig = console_mod.console
    console_mod.console = capture

    outcome   = "success"
    error_msg = ""
    try:
        app = DriftApplication(abs_config, config_path=config_path, sandbox=sandbox, manual_run=not sandbox)
        with st.spinner("Running analysis cycle…"):
            app.run_once()
    except Exception as exc:  # noqa: BLE001
        outcome   = "error"
        error_msg = str(exc)
    finally:
        console_mod.console = orig

    st.session_state["_ctrl_output"]      = buf.getvalue()
    st.session_state["_ctrl_outcome"]     = outcome
    st.session_state["_ctrl_error"]       = error_msg
    st.session_state["_ctrl_show_output"] = True
    st.cache_resource.clear()
    st.rerun()

