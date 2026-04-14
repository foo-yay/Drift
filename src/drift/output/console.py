from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from drift.config.models import AppConfig

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

