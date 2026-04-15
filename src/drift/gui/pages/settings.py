"""Settings — form-based Pydantic-validated config editor.

Renders each AppConfig section as an expandable form group.  On save the
entered values are validated by Pydantic before writing back to the YAML
file.  A validation error surfaces inline — the file is never overwritten
with invalid data.
"""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
import yaml

from drift.gui.state import get_config, project_root

_ROOT = project_root()


@st.cache_resource
def _load_config():
    return get_config()


def _config_path() -> Path:
    env = os.environ.get("DRIFT_CONFIG", "")
    if env and Path(env).exists():
        return Path(env)
    return _ROOT / "config" / "settings.yaml"


def page() -> None:
    st.title("🔧 Settings")
    st.caption(
        "Changes are validated by Pydantic before being written. "
        "The settings file is **not** modified unless all sections pass validation. "
        "Restart `drift gui` (or clear the Streamlit cache) for changes to take effect."
    )

    config = _load_config()

    # Read raw YAML so we can round-trip without losing non-model keys
    cfg_path = _config_path()
    with cfg_path.open("r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    edited: dict = {}
    validation_errors: list[str] = []

    # ------------------------------------------------------------------
    # App
    # ------------------------------------------------------------------
    with st.expander("App", expanded=False):
        app = raw.get("app", {})
        edited["app"] = {
            "name": st.text_input("Name", value=app.get("name", "Drift"), key="s_app_name"),
            "timezone": st.text_input("Timezone", value=app.get("timezone", "America/New_York"), key="s_app_tz"),
            "loop_interval_seconds": st.number_input(
                "Loop interval (seconds)", value=int(app.get("loop_interval_seconds", 900)),
                min_value=60, step=60, key="s_app_interval",
            ),
            "mode": st.selectbox(
                "Mode",
                ["paper-live", "sandbox", "dry-run", "replay", "llm-debug"],
                index=["paper-live", "sandbox", "dry-run", "replay", "llm-debug"].index(
                    app.get("mode", "paper-live")
                ),
                key="s_app_mode",
            ),
            "log_level": st.selectbox(
                "Log level",
                ["DEBUG", "INFO", "WARNING", "ERROR"],
                index=["DEBUG", "INFO", "WARNING", "ERROR"].index(app.get("log_level", "INFO")),
                key="s_app_log_level",
            ),
        }

    # ------------------------------------------------------------------
    # Instrument
    # ------------------------------------------------------------------
    with st.expander("Instrument", expanded=False):
        inst = raw.get("instrument", {})
        edited["instrument"] = {
            "symbol":      st.text_input("Symbol", value=inst.get("symbol", "MNQ"), key="s_inst_sym"),
            "allow_long":  st.checkbox("Allow long",  value=bool(inst.get("allow_long", True)), key="s_inst_long"),
            "allow_short": st.checkbox("Allow short", value=bool(inst.get("allow_short", True)), key="s_inst_short"),
        }

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    with st.expander("Sessions", expanded=False):
        sess = raw.get("sessions", {})
        blocks = sess.get("blocks", [{"start": "09:40", "end": "15:30"}])
        # For simplicity support editing up to 3 session blocks
        edited_blocks = []
        for idx in range(min(len(blocks), 3)):
            bc1, bc2 = st.columns(2)
            start = bc1.text_input(f"Block {idx+1} start (HH:MM)", value=blocks[idx].get("start", "09:40"), key=f"s_sess_start_{idx}")
            end   = bc2.text_input(f"Block {idx+1} end   (HH:MM)", value=blocks[idx].get("end", "15:30"),   key=f"s_sess_end_{idx}")
            if start and end:
                edited_blocks.append({"start": start, "end": end})

        edited["sessions"] = {
            "enabled": st.checkbox("Session gate enabled", value=bool(sess.get("enabled", True)), key="s_sess_enabled"),
            "blocks":  edited_blocks or blocks,
            "skip_first_n_minutes_after_open": st.number_input(
                "Skip first N minutes after open", value=int(sess.get("skip_first_n_minutes_after_open", 10)),
                min_value=0, step=1, key="s_sess_skip",
            ),
        }

    # ------------------------------------------------------------------
    # Risk
    # ------------------------------------------------------------------
    with st.expander("Risk", expanded=False):
        risk = raw.get("risk", {})
        rc1, rc2 = st.columns(2)
        edited["risk"] = {
            "min_confidence": rc1.number_input(
                "Min confidence (%)", value=int(risk.get("min_confidence", 60)),
                min_value=0, max_value=100, step=1, key="s_risk_conf",
            ),
            "min_reward_risk": rc2.number_input(
                "Min reward/risk", value=float(risk.get("min_reward_risk", 1.8)),
                min_value=0.1, step=0.1, format="%.1f", key="s_risk_rr",
            ),
            "max_signals_per_day": rc1.number_input(
                "Max signals/day", value=int(risk.get("max_signals_per_day", 3)),
                min_value=1, step=1, key="s_risk_max_sigs",
            ),
            "cooldown_minutes": rc2.number_input(
                "Cooldown (minutes)", value=int(risk.get("cooldown_minutes", 15)),
                min_value=0, step=1, key="s_risk_cooldown",
            ),
            "max_stop_points": rc1.number_input(
                "Max stop (points)", value=float(risk.get("max_stop_points", 40.0)),
                min_value=0.1, step=0.5, format="%.1f", key="s_risk_maxstop",
            ),
            "min_stop_points": rc2.number_input(
                "Min stop (points)", value=float(risk.get("min_stop_points", 6.0)),
                min_value=0.1, step=0.5, format="%.1f", key="s_risk_minstop",
            ),
            "atr_stop_floor_mult": rc1.number_input(
                "ATR stop floor mult", value=float(risk.get("atr_stop_floor_mult", 0.8)),
                min_value=0.1, step=0.1, format="%.2f", key="s_risk_atrstop",
            ),
            "atr_target_mult": rc2.number_input(
                "ATR target mult", value=float(risk.get("atr_target_mult", 1.8)),
                min_value=0.1, step=0.1, format="%.2f", key="s_risk_atrtgt",
            ),
            "max_hold_minutes_default": rc1.number_input(
                "Max hold (minutes)", value=int(risk.get("max_hold_minutes_default", 25)),
                min_value=1, step=1, key="s_risk_hold",
            ),
            "no_trade_during_high_impact_events": rc2.checkbox(
                "No trade during high-impact events",
                value=bool(risk.get("no_trade_during_high_impact_events", True)),
                key="s_risk_nohi",
            ),
        }

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------
    with st.expander("Gates", expanded=False):
        gates = raw.get("gates", {})
        gc1, gc2 = st.columns(2)
        edited["gates"] = {
            "regime_enabled": gc1.checkbox("Regime gate enabled", value=bool(gates.get("regime_enabled", True)), key="s_gates_regime"),
            "min_trend_score": gc1.number_input(
                "Min trend score", value=int(gates.get("min_trend_score", 35)),
                min_value=0, max_value=100, step=1, key="s_gates_trend",
            ),
            "min_momentum_score": gc2.number_input(
                "Min momentum score", value=int(gates.get("min_momentum_score", 30)),
                min_value=0, max_value=100, step=1, key="s_gates_mom",
            ),
            "block_on_extreme_volatility": gc2.checkbox(
                "Block on extreme volatility",
                value=bool(gates.get("block_on_extreme_volatility", True)),
                key="s_gates_volvol",
            ),
            "cooldown_enabled": gc1.checkbox("Cooldown gate enabled", value=bool(gates.get("cooldown_enabled", True)), key="s_gates_cd"),
            "kill_switch_enabled": gc2.checkbox("Kill switch gate enabled", value=bool(gates.get("kill_switch_enabled", True)), key="s_gates_ks"),
            "kill_switch_path": st.text_input("Kill switch path", value=gates.get("kill_switch_path", "data/.kill_switch"), key="s_gates_kspath"),
            "news_gate_enabled": gc1.checkbox("News gate enabled", value=bool(gates.get("news_gate_enabled", True)), key="s_gates_news"),
            "news_blackout_minutes": gc2.number_input(
                "News blackout (minutes)", value=int(gates.get("news_blackout_minutes", 30)),
                min_value=0, step=1, key="s_gates_newsblk",
            ),
        }

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------
    with st.expander("Economic Calendar", expanded=False):
        cal = raw.get("calendar", {})
        edited["calendar"] = {
            "enabled": st.checkbox("Calendar gate enabled", value=bool(cal.get("enabled", True)), key="s_cal_en"),
            "buffer_minutes_before": st.number_input(
                "Buffer before event (minutes)", value=int(cal.get("buffer_minutes_before", 20)),
                min_value=0, step=1, key="s_cal_before",
            ),
            "buffer_minutes_after": st.number_input(
                "Buffer after event (minutes)", value=int(cal.get("buffer_minutes_after", 10)),
                min_value=0, step=1, key="s_cal_after",
            ),
            "filter_countries": [
                c.strip()
                for c in st.text_input(
                    "Filter countries (comma-separated)",
                    value=", ".join(cal.get("filter_countries", ["USD"])),
                    key="s_cal_countries",
                ).split(",")
                if c.strip()
            ],
            "cache_ttl_minutes": st.number_input(
                "Cache TTL (minutes)", value=int(cal.get("cache_ttl_minutes", 60)),
                min_value=1, step=1, key="s_cal_ttl",
            ),
        }

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------
    with st.expander("LLM", expanded=False):
        llm = raw.get("llm", {})
        lc1, lc2 = st.columns(2)
        edited["llm"] = {
            "provider": lc1.text_input("Provider", value=llm.get("provider", "anthropic"), key="s_llm_prov"),
            "model":    lc2.text_input("Model",    value=llm.get("model", "claude-sonnet-4-6"), key="s_llm_model"),
            "temperature": lc1.number_input(
                "Temperature", value=float(llm.get("temperature", 0.1)),
                min_value=0.0, max_value=1.0, step=0.05, format="%.2f", key="s_llm_temp",
            ),
            "timeout_seconds": lc2.number_input(
                "Timeout (seconds)", value=int(llm.get("timeout_seconds", 30)),
                min_value=1, step=1, key="s_llm_timeout",
            ),
            "max_retries": lc1.number_input(
                "Max retries", value=int(llm.get("max_retries", 2)),
                min_value=0, step=1, key="s_llm_retries",
            ),
            "api_key_env": lc2.text_input(
                "API key env var", value=llm.get("api_key_env", "ANTHROPIC_API_KEY"),
                key="s_llm_apikey",
            ),
            "performance_context_enabled": lc1.checkbox(
                "Performance context enabled",
                value=bool(llm.get("performance_context_enabled", True)),
                key="s_llm_perfctx",
            ),
            "performance_context_lookback_days": lc2.number_input(
                "Perf context lookback (days)",
                value=int(llm.get("performance_context_lookback_days", 30)),
                min_value=1, step=1, key="s_llm_lookback",
            ),
            "few_shot_examples": lc1.number_input(
                "Few-shot examples", value=int(llm.get("few_shot_examples", 2)),
                min_value=0, max_value=10, step=1, key="s_llm_fewshot",
            ),
        }

    # Carry through unchanged sections
    for key in raw:
        if key not in edited:
            edited[key] = raw[key]

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    st.divider()
    col_save, col_reset = st.columns([1, 4])

    if col_save.button("💾 Save", type="primary", use_container_width=True):
        _save_config(edited, cfg_path)

    if col_reset.button("↺ Reload from file", use_container_width=True):
        st.cache_resource.clear()
        st.rerun()


def _save_config(edited: dict, cfg_path: Path) -> None:
    """Validate with Pydantic then write YAML atomically."""
    from drift.config.models import AppConfig
    from pydantic import ValidationError

    try:
        AppConfig.model_validate(edited)
    except ValidationError as exc:
        for err in exc.errors():
            field = " → ".join(str(l) for l in err["loc"])
            st.error(f"**{field}**: {err['msg']}", icon="🚫")
        return

    # Write atomically via a temp file
    tmp = cfg_path.with_suffix(".yaml.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.dump(edited, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
        tmp.replace(cfg_path)
    except OSError as exc:
        st.error(f"Could not write config file: {exc}")
        tmp.unlink(missing_ok=True)
        return

    st.success("Settings saved. Clear the Streamlit cache (or restart) to apply.", icon="✅")
    st.cache_resource.clear()

