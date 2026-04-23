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
    # Active Instrument
    # ------------------------------------------------------------------
    st.subheader("Active Instrument")
    st.caption(
        "Switch the instrument being analyzed and traded. "
        "Takes effect immediately — the scheduler restarts and uses the new instrument on its next cycle."
    )

    _active_json = _ROOT / "config" / "active_instrument.json"

    def _read_active_symbol(fallback: str) -> str:
        try:
            import json
            data = json.loads(_active_json.read_text(encoding="utf-8"))
            return data.get("symbol", fallback)
        except Exception:  # noqa: BLE001
            return fallback

    instruments = config.watched_instruments or []
    if not instruments:
        st.caption(
            f"Active: **{config.instrument.symbol}** "
            "— add `watched_instruments` to settings.yaml to enable switching"
        )
    else:
        symbols = [i.symbol for i in instruments]
        active_sym = _read_active_symbol(config.instrument.symbol)

        col_sel, col_btn = st.columns([3, 1])
        with col_sel:
            chosen = st.selectbox(
                "Instrument",
                options=symbols,
                index=symbols.index(active_sym) if active_sym in symbols else 0,
                label_visibility="collapsed",
                key="instrument_select",
            )
        with col_btn:
            changed = chosen != active_sym
            if st.button("Apply", key="instr_apply", type="primary", disabled=not changed):
                import json

                _active_json.parent.mkdir(parents=True, exist_ok=True)
                _active_json.write_text(
                    json.dumps({"symbol": chosen}), encoding="utf-8"
                )
                # Clear cached config so the controls page reflects the change.
                _load_config.clear()
                # Restart background scheduler so it picks up the new instrument.
                try:
                    from drift.gui.scheduler import restart_scheduler
                    restart_scheduler()
                except Exception:  # noqa: BLE001
                    pass
                st.success(
                    f"Switched to **{chosen}**. Scheduler restarting — next cycle will use the new instrument.",
                    icon="✅",
                )
                st.rerun()

        # Show details of the active instrument profile.
        active_profile = next((i for i in instruments if i.symbol == active_sym), None)
        if active_profile:
            st.caption(
                f"{active_profile.asset_class.capitalize()} · "
                f"tick={active_profile.tick_value:.2f} · "
                f"exchange={active_profile.exchange} · "
                f"long={'✅' if active_profile.allow_long else '❌'} "
                f"short={'✅' if active_profile.allow_short else '❌'}"
            )

    st.divider()

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

        if outcome in {"TRADE_PLAN_ISSUED", "LLM_NO_TRADE", "BLOCKED", "NO_DATA", "unknown"}:
            st.success("Cycle completed.", icon="✅")
        elif outcome == "error":
            st.error(f"Cycle failed: {error}", icon="🚨")

        if output:
            st.code(output, language=None)

        st.session_state.pop("_ctrl_show_output", None)

    st.divider()

    # ------------------------------------------------------------------
    # Scheduler status  (auto-refreshes every 10 s)
    # ------------------------------------------------------------------
    st.subheader("Scheduler")

    @st.fragment(run_every=10)
    def _scheduler_panel() -> None:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")

        try:
            from drift.gui.scheduler import ensure_scheduler_running
            scheduler = ensure_scheduler_running()
            alive = scheduler.is_alive()
            snap  = scheduler.state.snapshot()
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Scheduler status unavailable: {exc}")
            return

        # ── Health badge ──────────────────────────────────────────────
        if snap["running"]:
            st.warning("⏳ Cycle in progress…", icon=None)
        elif alive:
            st.success("● Scheduler running", icon="✅")
        else:
            st.error("● Scheduler stopped — restart `drift gui` to recover", icon="🚨")

        # ── Metric row ────────────────────────────────────────────────
        now_utc   = datetime.now(tz=timezone.utc)
        last_run  = snap["last_run_utc"]
        next_run  = snap["next_run_utc"]

        def _fmt_et(ts: datetime | None) -> str:
            if ts is None:
                return "—"
            return ts.astimezone(_ET).strftime("%b %-d, %H:%M:%S ET")

        def _elapsed(ts: datetime | None) -> str:
            if ts is None:
                return ""
            secs = int((now_utc - ts).total_seconds())
            if secs < 60:
                return f"{secs}s ago"
            mins, s = divmod(secs, 60)
            if mins < 60:
                return f"{mins}m {s:02d}s ago"
            h, m = divmod(mins, 60)
            return f"{h}h {m:02d}m ago"

        def _countdown(ts: datetime | None) -> str:
            if ts is None:
                return ""
            secs = int((ts - now_utc).total_seconds())
            if secs <= 0:
                return "overdue"
            if secs < 60:
                return f"in {secs}s"
            mins, s = divmod(secs, 60)
            return f"in {mins}m {s:02d}s"

        outcome = snap["last_outcome"]
        outcome_display = {
            "TRADE_PLAN_ISSUED": "📈 Trade Plan",
            "LLM_NO_TRADE":     "⏸ No Trade",
            "BLOCKED":          "🚫 Blocked",
            "NO_DATA":          "⚠️ No Data",
            "error":            "❌ Error",
        }.get(outcome, outcome or "—")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Last cycle", _fmt_et(last_run), delta=_elapsed(last_run), delta_color="off")
        c2.metric("Next cycle", _fmt_et(next_run), delta=_countdown(next_run), delta_color="off")
        c3.metric("Outcome", outcome_display)
        c4.metric("Cycles run", snap["cycle_count"])

        # ── Error detail ──────────────────────────────────────────────
        if snap["last_error"]:
            st.error(f"Last error: {snap['last_error']}", icon="🚨")

        # ── Interval info ─────────────────────────────────────────────
        try:
            cfg = _load_config()
            interval = cfg.app.loop_interval_seconds
            mins, secs = divmod(interval, 60)
            interval_str = f"{mins}m" if secs == 0 else f"{mins}m {secs}s"
            st.caption(f"Loop interval: {interval_str}  ·  Timestamps in ET  ·  Refreshes every 10 s")
        except Exception:  # noqa: BLE001
            pass

    _scheduler_panel()


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

    outcome   = "unknown"
    error_msg = ""
    try:
        app = DriftApplication(abs_config, config_path=config_path, sandbox=sandbox, manual_run=not sandbox, trigger="manual")
        with st.spinner("Running analysis cycle…"):
            outcome = app.run_once() or "unknown"
    except Exception as exc:  # noqa: BLE001
        outcome   = "error"
        error_msg = str(exc)
    finally:
        console_mod.console = orig

    st.session_state["_ctrl_output"]      = buf.getvalue()
    st.session_state["_ctrl_outcome"]     = outcome
    st.session_state["_ctrl_error"]       = error_msg
    st.session_state["_ctrl_show_output"] = True
    st.rerun()

