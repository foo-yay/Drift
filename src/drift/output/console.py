from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box as rich_box

from drift.config.models import AppConfig
from drift.models import MarketSnapshot

console = Console()


def render_startup(config: AppConfig, config_path: str) -> None:
    table = Table(show_header=False, box=None)
    table.add_row("App", config.app.name)
    table.add_row("Mode", config.app.mode)
    table.add_row("Symbol", config.instrument.symbol)
    table.add_row("Loop Interval", f"{config.app.loop_interval_seconds}s")
    table.add_row("Timezone", config.app.timezone)
    table.add_row("Config", config_path)
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


