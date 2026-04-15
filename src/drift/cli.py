from __future__ import annotations

from typing import Annotated

import typer
from dotenv import load_dotenv

load_dotenv()  # loads .env from cwd (or parent dirs) into os.environ

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
        typer.Option("--once/--loop", help="Run a single cycle then exit. Default is to loop continuously."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Bypass session gate and use mock LLM. Test outside market hours without spending API credits."),
    ] = False,
) -> None:
    config = load_app_config(config_path)
    application = DriftApplication(config=config, config_path=config_path, dry_run=dry_run)
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


@app.command()
def replay(
    config_path: Annotated[str, typer.Option("--config", help="Path to settings YAML.")] = (
        "config/settings.yaml"
    ),
    start: Annotated[str, typer.Option("--start", help="Start date YYYY-MM-DD. Defaults to yesterday.")] = "",
    end: Annotated[str, typer.Option("--end", help="End date YYYY-MM-DD. Defaults to yesterday.")] = "",
    csv_1m: Annotated[str, typer.Option("--csv-1m", help="Path to pre-downloaded 1m bar CSV.")] = "",
    csv_5m: Annotated[str, typer.Option("--csv-5m", help="Path to pre-downloaded 5m bar CSV.")] = "",
    csv_1h: Annotated[str, typer.Option("--csv-1h", help="Path to pre-downloaded 1h bar CSV.")] = "",
    step: Annotated[int, typer.Option("--step", help="Fire the pipeline every N 1m bars (default 15 = every 15 minutes).")] = 15,
    disable_session_gate: Annotated[bool, typer.Option("--disable-session-gate", help="Allow signals outside RTH hours.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="Print the full snapshot panel on each step.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Use mock LLM — no API calls, no credits spent.")] = False,
    export_csv: Annotated[str, typer.Option("--export-csv", help="Write signal outcomes to a CSV file, e.g. logs/replay.csv.")] = "",
) -> None:
    """Replay historical bars through the full Drift pipeline.

    With no arguments, replays yesterday's session fetched from yfinance.

    \b
    Data source options:

    \b
    1. Default — yesterday from yfinance:
         drift replay

    \b
    2. Specific date range from yfinance (last 7 days only for 1m):
         drift replay --start 2026-04-10 --end 2026-04-11

    \b
    3. Pre-downloaded CSVs (no date limit):
         drift replay --csv-1m data/MNQ_1m.csv --csv-5m data/MNQ_5m.csv --csv-1h data/MNQ_1h.csv
    """
    from datetime import date, timedelta

    from drift.ai.client import LLMClient
    from drift.ai.mock_client import MockLLMClient
    from drift.output.console import render_replay_summary
    from drift.replay.engine import ReplayEngine
    from drift.replay.loader import fetch_bars_for_date_range, load_bars_from_csv

    config = load_app_config(config_path)
    symbol = config.instrument.symbol

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if dry_run:
        llm_client = MockLLMClient()
        console.print("[yellow]DRY RUN — mock LLM active. No API credits will be spent.[/yellow]")
    elif api_key:
        llm_client = LLMClient(config.llm)
    else:
        llm_client = MockLLMClient()
        console.print("[yellow]No ANTHROPIC_API_KEY found — using mock LLM. Signals are not real.[/yellow]")

    # ------------------------------------------------------------------
    # Load bars
    # ------------------------------------------------------------------
    use_csv = csv_1m or csv_5m or csv_1h
    use_dates = start or end

    if use_csv and use_dates:
        console.print("[bold red]ERROR[/bold red] Provide either --start/--end or --csv-*, not both.")
        raise typer.Exit(1)

    if use_csv:
        missing = [f for f, v in [("--csv-1m", csv_1m), ("--csv-5m", csv_5m), ("--csv-1h", csv_1h)] if not v]
        if missing:
            console.print(f"[bold red]ERROR[/bold red] Missing CSV paths: {', '.join(missing)}")
            raise typer.Exit(1)
        console.rule(f"[bold]Drift Replay — {symbol} (CSV)[/bold]")
        try:
            bars_1m, bars_5m, bars_1h = load_bars_from_csv(csv_1m, csv_5m, csv_1h, symbol)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            console.print(f"[bold red]ERROR[/bold red] Failed to load CSVs: {exc}")
            raise typer.Exit(1) from exc
    else:
        # Default: yesterday. Skip back over weekends (Sat→Fri, Sun→Fri).
        if not start and not end:
            yesterday = date.today() - timedelta(days=1)
            if yesterday.weekday() == 5:   # Saturday
                yesterday -= timedelta(days=1)
            elif yesterday.weekday() == 6:  # Sunday
                yesterday -= timedelta(days=2)
            start = end = str(yesterday)

        if not start or not end:
            console.print("[bold red]ERROR[/bold red] Both --start and --end are required when fetching from yfinance.")
            raise typer.Exit(1)
        console.rule(f"[bold]Drift Replay — {symbol} ({start} → {end})[/bold]")
        console.print("[dim]Fetching bars from yfinance...[/dim]")
        try:
            bars_1m, bars_5m, bars_1h = fetch_bars_for_date_range(symbol, start, end)
        except ValueError as exc:
            console.print(f"[bold red]ERROR[/bold red] {exc}")
            raise typer.Exit(1) from exc

    console.print(
        f"Loaded [bold]{len(bars_1m)}[/bold] 1m bars, "
        f"[bold]{len(bars_5m)}[/bold] 5m bars, "
        f"[bold]{len(bars_1h)}[/bold] 1h bars"
    )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    engine = ReplayEngine(
        config=config,
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_1h=bars_1h,
        llm_client=llm_client,
        step_every_n_bars=step,
        disable_session_gate=disable_session_gate,
        verbose=verbose,
    )

    summary = engine.run()
    render_replay_summary(summary)

    if export_csv:
        from drift.replay.csv_export import export_replay_csv
        export_replay_csv(summary, export_csv)
        console.print(f"[green]CSV exported → {export_csv}[/green]")


@app.command("replay-gui")
def replay_gui() -> None:
    """Launch the Streamlit visual replay frontend in a local browser."""
    import shutil
    import subprocess
    from pathlib import Path

    app_path = Path(__file__).parent / "replay" / "streamlit_app.py"

    # Derive the project root from cli.py's location:
    #   src/drift/cli.py → parents[0]=drift, parents[1]=src, parents[2]=project root
    # The venv's streamlit is always at <project_root>/.venv/bin/streamlit.
    # This is immune to sys.executable and PATH resolving to a different Python.
    project_root = Path(__file__).parents[2]
    streamlit_bin = project_root / ".venv" / "bin" / "streamlit"

    if not streamlit_bin.exists():
        # Fallback: honour PATH (e.g. if the user uses a custom venv name)
        found = shutil.which("streamlit")
        if found:
            streamlit_bin = Path(found)

    if not streamlit_bin.exists():
        console.print(
            "[bold red]ERROR[/bold red] streamlit not found in .venv/bin/. "
            "Run: .venv/bin/pip install streamlit plotly"
        )
        raise typer.Exit(1)

    result = subprocess.run([str(streamlit_bin), "run", str(app_path)], check=False)
    raise typer.Exit(result.returncode)


if __name__ == "__main__":
    app()
