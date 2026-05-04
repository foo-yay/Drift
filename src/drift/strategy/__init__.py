"""Strategy layer — deterministic setup families.

Each strategy module is self-contained: it accepts raw bars, applies configurable
thresholds, and returns a SetupResult (LONG / SHORT / NO_TRADE) that bypasses
the LLM when a clean deterministic setup is found.

Current strategies:
    sweep_scanner  — liquidity sweep + FVG + pin bar confirmation
"""
