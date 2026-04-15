"""Drift Dashboard — read-only viewer for the Drift event log.

Run from the project root:
    streamlit run src/drift/replay/streamlit_app.py

Or via the CLI alias:
    drift replay-gui

The GUI is a pure viewer — all signal generation stays in the CLI:
    drift run                  live trading loop
    drift replay               historical replay

Pending live signals are resolved automatically on startup and on Refresh Log.
"""
from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env before any module that reads ANTHROPIC_API_KEY.
# Streamlit runs this file directly (bypassing cli.py where load_dotenv() normally fires).
load_dotenv()

import streamlit as st

from drift.replay.chart import build_equity_chart, events_to_df
from drift.storage.backfill import backfill_outcomes
from drift.storage.reader import load_events_from_log

# ------------------------------------------------------------------
# Page config (must be first Streamlit call)
# ------------------------------------------------------------------
st.set_page_config(
    page_title="Drift — Dashboard",
    page_icon="📈",
    layout="wide",
)

_LOG_PATH = Path(__file__).parents[3] / "logs" / "events.jsonl"
_ET       = ZoneInfo("America/New_York")





# ------------------------------------------------------------------
# Trade detail dialog
# ------------------------------------------------------------------

@st.dialog("Signal Detail", width="large")
def _show_signal_detail(event) -> None:
    tp    = event.trade_plan or {}
    out   = event.replay_outcome or {}
    gates = event.pre_gate_report or {}
    llm   = event.llm_decision_parsed or {}

    source      = getattr(event, "source", "live")
    ts_str      = event.event_time.astimezone(_ET).strftime("%Y-%m-%d %H:%M ET")
    outcome_lbl = out.get("outcome", "PENDING")
    st.caption(
        f"{ts_str}  •  {event.symbol}  •  source: **{source}**  •  outcome: **{outcome_lbl}**"
    )

    col_left, col_right = st.columns(2)

    with col_left:
        st.markdown("**Trade Plan**")
        st.json({
            "bias":          tp.get("bias"),
            "setup_type":    tp.get("setup_type"),
            "confidence":    tp.get("confidence"),
            "entry_zone":   f"{tp.get('entry_min')} – {tp.get('entry_max')}",
            "stop_loss":     tp.get("stop_loss"),
            "take_profit_1": tp.get("take_profit_1"),
            "take_profit_2": tp.get("take_profit_2"),
            "reward_risk":   tp.get("reward_risk_ratio"),
            "max_hold_min":  tp.get("max_hold_minutes"),
            "thesis":        tp.get("thesis"),
        })
        st.markdown("**LLM Decision**")
        st.json({k: v for k, v in llm.items() if k not in ("entry_zone", "do_not_trade_if")})

    with col_right:
        st.markdown("**Outcome**")
        st.json(out)
        st.markdown("**Gate Report**")
        st.json(gates)


# ------------------------------------------------------------------
# Auto-load events from the JSONL log (runs backfill first)
# ------------------------------------------------------------------

_SYMBOL = "MNQ=F"  # resolved from config; hardcoded here to avoid loading config on every rerun


def _load_events() -> tuple[list, int]:
    """Backfill any pending live signals then return (events, resolved_count)."""
    if not _LOG_PATH.exists():
        return [], 0
    try:
        resolved, _ = backfill_outcomes(_LOG_PATH, _SYMBOL)
    except Exception:  # noqa: BLE001 — never crash the GUI
        resolved = 0
    return load_events_from_log(_LOG_PATH), resolved


if "all_events" not in st.session_state:
    st.session_state["all_events"], _startup_resolved = _load_events()
    st.session_state["_startup_resolved"] = _startup_resolved


# ------------------------------------------------------------------
# Sidebar — filters and log stats
# ------------------------------------------------------------------

with st.sidebar:
    st.title("📈 Drift")
    st.divider()

    if st.button("🔄 Refresh Log", type="primary", use_container_width=True):
        with st.spinner("Resolving pending signals…"):
            events, resolved = _load_events()
        st.session_state["all_events"] = events
        st.session_state["_refresh_resolved"] = resolved
        for _k in ("selected_idx", "_dialog_shown_for", "page"):
            st.session_state.pop(_k, None)
        st.rerun()

    _all_tpi = [
        e for e in st.session_state["all_events"]
        if e.final_outcome == "TRADE_PLAN_ISSUED" and e.trade_plan
    ]

    st.subheader("Date Filter")
    if _all_tpi:
        _dates = sorted({e.event_time.astimezone(_ET).date() for e in _all_tpi})
        _min_d, _max_d = _dates[0], _dates[-1]
        start_f = st.date_input("From", _min_d, min_value=_min_d, max_value=_max_d, key="f_start")
        end_f   = st.date_input("To",   _max_d, min_value=_min_d, max_value=_max_d, key="f_end")
    else:
        start_f = end_f = None
        st.caption("No signals in log yet.")

    st.subheader("Source")
    show_live    = st.checkbox("Live",    value=True,  key="show_live")
    show_replay  = st.checkbox("Replay",  value=True,  key="show_replay")
    show_dry_run = st.checkbox("Dry-run", value=False, key="show_dry_run")

    st.divider()

    # Log summary
    _by_src: dict[str, int] = {}
    for _e in _all_tpi:
        _s = getattr(_e, "source", "live")
        _by_src[_s] = _by_src.get(_s, 0) + 1
    _resolved = sum(1 for e in _all_tpi if e.replay_outcome)
    _pending  = sum(1 for e in _all_tpi if not e.replay_outcome)
    st.caption(f"**Log:** {len(st.session_state['all_events'])} events total")
    for _s, _cnt in sorted(_by_src.items()):
        st.caption(f"  • {_s}: {_cnt} signal(s)")
    st.caption(f"✅ {_resolved} resolved  |  📌 {_pending} pending")
    st.divider()
    st.caption(
        "Generate signals via CLI:\n"
        "```\ndrift run\ndrift replay\n```\n"
        "Pending outcomes are resolved automatically on refresh."
    )


# ------------------------------------------------------------------
# Apply filters
# ------------------------------------------------------------------

# Show a toast when backfill resolved new outcomes (startup or refresh)
_notify_resolved = st.session_state.pop("_refresh_resolved", None)
if _notify_resolved is None:
    _notify_resolved = st.session_state.pop("_startup_resolved", None)
if _notify_resolved:
    st.toast(f"✅ Resolved {_notify_resolved} pending signal(s) automatically.", icon="✅")


def _src_ok(e) -> bool:
    src = getattr(e, "source", "live")
    if src == "live"    and not st.session_state.get("show_live",    True):  return False
    if src == "replay"  and not st.session_state.get("show_replay",  True):  return False
    if src == "dry_run" and not st.session_state.get("show_dry_run", False): return False
    return True


trade_events: list = [
    e for e in st.session_state["all_events"]
    if e.final_outcome == "TRADE_PLAN_ISSUED"
    and e.trade_plan
    and _src_ok(e)
]

if start_f and end_f:
    trade_events = [
        e for e in trade_events
        if start_f <= e.event_time.astimezone(_ET).date() <= end_f
    ]


# ------------------------------------------------------------------
# Page title + metric cards
# ------------------------------------------------------------------

st.title("Drift — Signal Dashboard")

if not st.session_state["all_events"]:
    st.warning(
        f"No events found in `{_LOG_PATH}`.  "
        "Run `drift run` or `drift replay` first, then click **🔄 Refresh Log**."
    )
    st.stop()

resolved_events = [e for e in trade_events if e.replay_outcome]
total_pnl = sum(
    float((e.replay_outcome or {}).get("pnl_points", 0.0)) for e in resolved_events
)
pnl_sign = "+" if total_pnl >= 0 else ""
wins = sum(
    1 for e in resolved_events
    if (e.replay_outcome or {}).get("outcome", "") in ("TP1_HIT", "TP2_HIT")
)
win_rate = round(wins / len(resolved_events) * 100, 1) if resolved_events else 0.0

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Signals",  len(trade_events))
c2.metric("Resolved", len(resolved_events))
c3.metric("Pending",  len(trade_events) - len(resolved_events))
c4.metric("Win Rate", f"{win_rate}%")
c5.metric("Total PnL", f"{pnl_sign}{total_pnl:.1f} pts")

st.divider()


# ------------------------------------------------------------------
# Equity curve — clickable scatter markers
# ------------------------------------------------------------------

fig, _resolved_idx_map = build_equity_chart(trade_events)
chart_event = st.plotly_chart(
    fig, on_select="rerun", selection_mode="points", use_container_width=True
)

# Collect dialog trigger from chart click (resolved by customdata = trade_events index)
_dialog_trigger: tuple | None = None

if chart_event and chart_event.selection and chart_event.selection.points:
    _pt  = chart_event.selection.points[0]
    _raw = _pt.get("customdata")
    if _raw is not None:
        _cidx = int(_raw[0]) if isinstance(_raw, (list, tuple)) else int(_raw)
        _ckey = ("chart", _cidx)
        if st.session_state.get("_dialog_shown_for") != _ckey:
            _dialog_trigger = (_ckey, trade_events[_cidx])

st.divider()


# ------------------------------------------------------------------
# Signal table — paginated, selectable rows
# ------------------------------------------------------------------

df = events_to_df(trade_events)

if df.empty:
    st.info("No trade signals match the current filters.")
    st.stop()

_PAGE_SIZE   = 25
_total_pages = max(1, (len(df) - 1) // _PAGE_SIZE + 1)
if st.session_state.get("page", 0) >= _total_pages:
    st.session_state["page"] = 0
_page  = st.session_state.get("page", 0)
_start = _page * _PAGE_SIZE
_end   = min(_start + _PAGE_SIZE, len(df))

st.subheader(f"Signals ({len(df)})")
_c1, _c2, _c3 = st.columns([1, 4, 1])
with _c1:
    if st.button("← Prev", disabled=(_page == 0), key="pg_prev"):
        st.session_state["page"] = _page - 1
        st.session_state.pop("_dialog_shown_for", None)
        st.rerun()
with _c2:
    st.caption(f"Page {_page + 1} of {_total_pages}  ({_start + 1}–{_end} of {len(df)})")
with _c3:
    if st.button("Next →", disabled=(_end >= len(df)), key="pg_next"):
        st.session_state["page"] = _page + 1
        st.session_state.pop("_dialog_shown_for", None)
        st.rerun()

selection = st.dataframe(
    df.iloc[_start:_end],
    use_container_width=True,
    selection_mode="single-row",
    on_select="rerun",
    key="signal_table",
    hide_index=True,
)

_sel_rows = selection.selection.rows if selection and selection.selection else []
if _sel_rows:
    _tidx = _start + _sel_rows[0]
    _tkey = ("table", _tidx)
    if st.session_state.get("_dialog_shown_for") != _tkey:
        _dialog_trigger = (_tkey, trade_events[_tidx])


# ------------------------------------------------------------------
# Open signal detail dialog (at most once per rerun)
# ------------------------------------------------------------------

if _dialog_trigger:
    st.session_state["_dialog_shown_for"] = _dialog_trigger[0]
    _show_signal_detail(_dialog_trigger[1])
