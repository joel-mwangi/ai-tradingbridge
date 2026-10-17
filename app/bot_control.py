"""Bot control module — re-exports schema models from bot_schema.

The DB-queue execution pattern (bot_runs table, worker heartbeats, _require_live_worker)
has been replaced by the FastAPI inline bot executor in app/bot_runner.py.
This module is kept only for backwards-compatible imports.
"""
from app.bot_schema import (  # noqa: F401  re-exported for backwards compat
    CONTRACTS,
    CreateRunRequest,
    DemoBotConfig,
    FixedStakeConfig,
    StopCondition,
)
