from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box as rich_box

from drift.config.models import AppConfig
from drift.models import GateResult, LLMDecision, MarketSnapshot, TradePlan

console = Console()


def render_startup(config: AppConfig, config_path: str, sandbox: bool = False) -> None:
    table = Table(show_header=False, box=None)
    table.add_row("App", config.app.name)
    table.add_row("Mode", config.app.mode)
    table.add_row("Symbol", config.instrument.symbol)
    table.add_row("Loop Interval", f"{config.app.loop_interval_seconds}s")
    table.add_row("Timezone", config.app.timezone)
    table.add_row("Config", config_path)
    if sandbox:
        table.add_row("[bold yellow]SANDBOX[/bold yellow]", "[yellow]session gate bypassed — mock LLM — isolated storage[/yellow]")
    console.print(Panel(table, title="Drift Startup", expand=False))


def render_status(message: str) -> None:
    console.print(f"[bold cyan]Drift[/bold cyan] {message}")


def render_success(message: str) -> None:
    console.print(f"[bold green]OK[/bold green] {message}")


def render_snapshot(snapshot: MarketSnapshot) -> None:
    """Render a MarketSnapshot as a two-column rich panel."""

    def _score_color(value: int) -> str:
        if value >= 65:
            return "green"
        if value >= 40:
            return "yellow"
        return "red"

    def _risk_color(value: int) -> str:
        """Invert — high extension/reversion risk is bad."""
        if value >= 65:
            return "red"
        if value >= 40:
            return "yellow"
        return "green"

    def _state_color(state: str) -> str:
        s = state.lower()
        if "bullish" in s:
            return "green"
        if "bearish" in s:
            return "red"
        if "elevated" in s or "extreme" in s:
            return "yellow"
        return "dim"

    scores = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold")
    scores.add_column("Score", style="dim", min_width=22)
    scores.add_column("Value", justify="right", min_width=6)

    score_fields = [
        ("trend_score", "Trend", _score_color),
        ("momentum_score", "Momentum", _score_color),
        ("volatility_score", "Volatility", _score_color),
        ("structure_quality", "Structure Quality", _score_color),
        ("pullback_quality", "Pullback Quality", _score_color),
        ("breakout_quality", "Breakout Quality", _score_color),
        ("extension_risk", "Extension Risk", _risk_color),
        ("mean_reversion_risk", "Mean Rev. Risk", _risk_color),
        ("session_alignment", "Session Align.", _score_color),
    ]
    for field, label, color_fn in score_fields:
        raw = getattr(snapshot, field)
        color = color_fn(raw)
        scores.add_row(label, f"[{color}]{raw}[/{color}]")

    context = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold")
    context.add_column("Context", style="dim", min_width=22)
    context.add_column("Value", min_width=20)

    state_fields = [
        ("short_trend_state", "Short Trend"),
        ("medium_trend_state", "Medium Trend"),
        ("momentum_state", "Momentum"),
        ("volatility_regime", "Volatility Regime"),
    ]
    for field, label in state_fields:
        raw = str(getattr(snapshot, field))
        color = _state_color(raw)
        context.add_row(label, f"[{color}]{raw}[/{color}]")

    context.add_row("Last Price", f"[bold]{snapshot.last_price:,.2f}[/bold]")
    context.add_row("Session", snapshot.session)
    context.add_row(
        "Bars (1m / 5m / 1h)",
        f"{snapshot.bars_1m_count} / {snapshot.bars_5m_count} / {snapshot.bars_1h_count}",
    )
    if snapshot.atr is not None:
        context.add_row("ATR (14)", f"{snapshot.atr:.2f} pts")
    context.add_row("As Of", snapshot.as_of.strftime("%H:%M:%S UTC"))

    if snapshot.market_note:
        note_panel = Panel(
            snapshot.market_note,
            title="Market Note",
            border_style="dim",
            expand=False,
        )
    else:
        note_panel = None

    outer = Table.grid(padding=(0, 2))
    outer.add_column()
    outer.add_column()
    outer.add_row(scores, context)

    console.print(Panel(outer, title=f"[bold]MarketSnapshot — {snapshot.symbol}[/bold]", expand=False))
    if note_panel:
        console.print(note_panel)


def render_gate_result(result: GateResult) -> None:
    """Render a single gate evaluation result."""
    if result.passed:
        icon = "[bold green]PASS[/bold green]"
    else:
        icon = "[bold red]BLOCK[/bold red]"
    console.print(f"  Gate [{result.gate_name}] {icon} — {result.reason}")


def render_gate_blocked(result: GateResult) -> None:
    """Render a full blocking panel when a gate vetoes the signal."""
    console.print(
        Panel(
            f"[bold red]{result.reason}[/bold red]",
            title=f"[bold red]Signal Blocked — {result.gate_name} gate[/bold red]",
            border_style="red",
            expand=False,
        )
    )


def render_llm_decision(decision: LLMDecision) -> None:
    """Render the raw LLM adjudication result."""
    color = "green" if decision.decision == "LONG" else "red" if decision.decision == "SHORT" else "yellow"
    table = Table(show_header=False, box=None)
    table.add_row("Decision", f"[bold {color}]{decision.decision}[/bold {color}]")
    table.add_row("Confidence", f"{decision.confidence}/100")
    table.add_row("Setup", decision.setup_type)
    table.add_row("Entry Style", decision.entry_style)
    table.add_row("Thesis", decision.thesis)
    table.add_row("Invalidation", decision.invalidation_hint)
    table.add_row("Hold", f"{decision.hold_minutes} min")
    console.print(Panel(table, title="LLM Decision", border_style=color, expand=False))


def render_no_trade(decision: LLMDecision, reason: str) -> None:
    """Render a NO_TRADE outcome with reason."""
    console.print(
        Panel(
            f"[yellow]{reason}[/yellow]\n[dim]{decision.thesis}[/dim]",
            title="[bold yellow]NO TRADE[/bold yellow]",
            border_style="yellow",
            expand=False,
        )
    )


def render_trade_plan(plan: TradePlan) -> None:
    """Render the full operator-facing trade plan."""
    color = "green" if plan.bias == "LONG" else "red"

    header = Table(show_header=False, box=None)
    header.add_row("Instrument", f"[bold]{plan.symbol}[/bold]")
    header.add_row("Bias", f"[bold {color}]{plan.bias}[/bold {color}]")
    header.add_row("Confidence", f"{plan.confidence}/100")
    header.add_row("Setup", plan.setup_type)
    header.add_row("", "")
    header.add_row("Entry Zone", f"[bold]{plan.entry_min:.2f} – {plan.entry_max:.2f}[/bold]")
    header.add_row("Stop Loss", f"[bold red]{plan.stop_loss:.2f}[/bold red]")
    header.add_row("TP1", f"[bold green]{plan.take_profit_1:.2f}[/bold green]")
    if plan.take_profit_2:
        header.add_row("TP2", f"[bold green]{plan.take_profit_2:.2f}[/bold green]")
    header.add_row("R:R", f"{plan.reward_risk_ratio:.1f}:1")
    header.add_row("Max Hold", f"{plan.max_hold_minutes} min")
    if plan.chase_above_below:
        chase_label = "Chase Above" if plan.bias == "LONG" else "Chase Below"
        header.add_row(chase_label, f"[dim]{plan.chase_above_below:.2f}[/dim]")

    console.print(Panel(header, title=f"[bold {color}]SIGNAL — {plan.symbol}[/bold {color}]", border_style=color, expand=False))

    console.print(Panel(plan.thesis, title="Thesis", border_style="dim", expand=False))

    inst_text = "\n".join(f"  {i + 1}. {line}" for i, line in enumerate(plan.operator_instructions))
    console.print(Panel(inst_text, title="[bold]Operator Instructions[/bold]", border_style="cyan", expand=False))

    if plan.do_not_trade_if:
        dnti_text = "\n".join(f"  • {c}" for c in plan.do_not_trade_if)
        console.print(Panel(dnti_text, title="[bold yellow]Do Not Trade If[/bold yellow]", border_style="yellow", expand=False))

    invalid_text = "\n".join(f"  • {c}" for c in plan.invalidation_conditions)
    console.print(Panel(invalid_text, title="Invalidation Conditions", border_style="dim", expand=False))


def render_replay_summary(summary: "ReplaySummary") -> None:  # type: ignore[name-defined]
    """Render a replay run summary table."""
    table = Table(show_header=False, box=None)
    table.add_row("Total 1m bars stepped", str(summary.total_steps))
    table.add_row("Pipeline evaluations", str(summary.pipeline_steps))
    table.add_row("Blocked by gates", str(summary.blocked))
    table.add_row("LLM NO_TRADE", str(summary.llm_no_trade))
    table.add_row(
        "[bold]Trade plans issued[/bold]",
        f"[bold]{summary.trade_plans_issued}[/bold]",
    )
    table.add_row("Signal rate", f"{summary.signal_rate_pct}%")

    if summary.outcomes_resolved > 0:
        table.add_row("", "")  # spacer
        table.add_row(
            "[bold green]TP1 hits[/bold green]",
            f"[bold green]{summary.tp1_hits}[/bold green]",
        )
        table.add_row(
            "[bold bright_green]TP2 hits[/bold bright_green]",
            f"[bold bright_green]{summary.tp2_hits}[/bold bright_green]",
        )
        table.add_row(
            "[bold red]Stop hits[/bold red]",
            f"[bold red]{summary.stop_hits}[/bold red]",
        )
        table.add_row("Time stops", str(summary.time_stops))
        table.add_row("Session ends", str(summary.session_ends))
        table.add_row(
            "[bold]Win rate[/bold]",
            f"[bold]{summary.win_rate_pct}%[/bold]  "
            f"({summary.tp1_hits + summary.tp2_hits}W / {summary.stop_hits}L)",
        )

    console.print(Panel(table, title="[bold]Replay Summary[/bold]", border_style="cyan", expand=False))

