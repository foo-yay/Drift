from __future__ import annotations

from typing import Annotated

import typer

from drift.app import DriftApplication
from drift.output.console import console, render_success
from drift.utils.config import load_app_config

app = typer.Typer(help="Drift: local MNQ signal bot scaffold.")


@app.command("validate-config")
def validate_config(
    config_path: Annotated[str, typer.Option("--config", help="Path to settings YAML.")] = (
        "config/settings.yaml"
    ),
) -> None:
    load_app_config(config_path)
    render_success(f"validated {config_path}")


@app.command()
def run(
    config_path: Annotated[str, typer.Option("--config", help="Path to settings YAML.")] = (
        "config/settings.yaml"
    ),
    once: Annotated[
        bool,
        typer.Option("--once/--loop", help="Run a single cycle or continue looping."),
    ] = True,
) -> None:
    config = load_app_config(config_path)
    application = DriftApplication(config=config, config_path=config_path)
    if once:
        application.run_once()
        return
    application.run_forever()


@app.command("fetch-data")
def fetch_data(
    config_path: Annotated[str, typer.Option("--config", help="Path to settings YAML.")] = (
        "config/settings.yaml"
    ),
) -> None:
    """Fetch a live data sample via yfinance and print a summary. Use this to verify connectivity."""
    from rich.table import Table

    from drift.data.providers.yfinance_provider import YFinanceProvider
    from drift.features.engine import FeatureEngine

    config = load_app_config(config_path)
    symbol = config.instrument.symbol
    provider = YFinanceProvider()
    engine = FeatureEngine(config)

    console.rule(f"[bold]Drift — Data Fetch Test ({symbol})[/bold]")

    # Latest quote
    try:
        price = provider.get_latest_quote(symbol)
        render_success(f"latest quote: {price:,.2f}")
    except ValueError as exc:
        console.print(f"[bold red]FAIL[/bold red] latest quote: {exc}")
        raise typer.Exit(1) from exc

    # Session status
    session = provider.get_session_status(symbol)
    market_open = provider.is_market_open(symbol)
    render_success(f"session: {session!r}  |  market open: {market_open}")

    # Bars per timeframe
    bars_by_tf: dict[str, list] = {}
    for timeframe, lookback in [
        ("1m", config.lookbacks.bars_1m),
        ("5m", config.lookbacks.bars_5m),
        ("1h", config.lookbacks.bars_1h),
    ]:
        bars = provider.get_recent_bars(symbol, timeframe, lookback)
        bars_by_tf[timeframe] = bars
        if not bars:
            console.print(f"[bold yellow]WARN[/bold yellow] {timeframe}: no bars returned")
            continue

        first, last = bars[0], bars[-1]
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_row("timeframe", timeframe)
        table.add_row("bars returned", str(len(bars)))
        table.add_row("oldest bar", str(first.timestamp))
        table.add_row("newest bar", str(last.timestamp))
        table.add_row("last close", f"{last.close:,.2f}")
        console.print(table)
        console.print()

    # Feature engine snapshot
    console.rule("[bold]Feature Engine — MarketSnapshot[/bold]")
    snapshot = engine.compute(
        bars_1m=bars_by_tf.get("1m", []),
        bars_5m=bars_by_tf.get("5m", []),
        bars_1h=bars_by_tf.get("1h", []),
        last_price=price,
        session=session,
    )

    score_table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    score_table.add_column("Field", style="dim")
    score_table.add_column("Value")

    for field, value in snapshot.model_dump().items():
        score_table.add_row(field, str(value))

    console.print(score_table)


@app.command()
def kill(
    config_path: Annotated[str, typer.Option("--config", help="Path to settings YAML.")] = (
        "config/settings.yaml"
    ),
) -> None:
    """Activate the kill switch — all signals will be blocked immediately."""
    from pathlib import Path

    config = load_app_config(config_path)
    path = Path(config.gates.kill_switch_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    console.print(f"[bold red]KILL SWITCH ACTIVE[/bold red] — signals blocked. ({path})")
    console.print("Run [bold]drift resume[/bold] to re-enable.")


@app.command()
def resume(
    config_path: Annotated[str, typer.Option("--config", help="Path to settings YAML.")] = (
        "config/settings.yaml"
    ),
) -> None:
    """Deactivate the kill switch — signals will resume on the next cycle."""
    from pathlib import Path

    config = load_app_config(config_path)
    path = Path(config.gates.kill_switch_path)
    if path.exists():
        path.unlink()
        console.print(f"[bold green]KILL SWITCH CLEARED[/bold green] — signals re-enabled. ({path})")
    else:
        console.print("[dim]Kill switch was not active.[/dim]")


if __name__ == "__main__":
    app()
