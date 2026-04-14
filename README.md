# Drift

Drift is a local Python CLI for generating auditable, manual MNQ trade plans from deterministic market analysis plus an AI adjudication layer.

The current scaffold implements the Phase 1 foundation from `implementation plan.md`:

- typed YAML configuration
- CLI entrypoints
- core domain models
- market data provider abstraction
- console output helpers
- JSONL event logging
- baseline tests

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
drift validate-config
drift run --once
```

`config/settings.yaml` defaults to `dry-run` so the app is runnable before a live data provider or LLM client is connected.

## Project layout

```text
src/drift/             Python package
config/settings.yaml   runtime settings
config/prompts.yaml    LLM prompt scaffolding
tests/                 baseline test suite
implementation plan.md product specification from the user
```

## Current status

The repository is intentionally scaffolded around a safe first slice:

- no broker integration
- no live data provider implementation yet
- no autonomous execution

The next recommended step is implementing deterministic bar ingestion, indicators, and market snapshot construction.

