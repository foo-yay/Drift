from __future__ import annotations

from typing import Annotated

import typer

from drift.app import DriftApplication
from drift.output.console import render_success
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


if __name__ == "__main__":
    app()
