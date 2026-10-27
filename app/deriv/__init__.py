"""Deriv gateway package.

Assembles the ``/deriv`` APIRouter from focused submodules: credentials storage,
request models, security helpers, HTTP routes, and the two WebSocket proxies.
Importing this package registers every route on the exported ``router``.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/deriv", tags=["deriv"])

# Importing the submodules registers their routes/decorators on ``router``.
# Order matters only in that leaf helpers (credentials/models/security) must be
# importable before the route modules that depend on them.
from app.deriv import credentials  # noqa: E402,F401
from app.deriv import models  # noqa: E402,F401
from app.deriv import security  # noqa: E402,F401  (registers POST /ws-ticket)
from app.deriv import routes  # noqa: E402,F401  (registers HTTP routes)
from app.deriv import trading_session  # noqa: E402,F401
from app.deriv import trading_proxy  # noqa: E402,F401  (registers WS /ws)
from app.deriv import multipliers_proxy  # noqa: E402,F401  (registers WS /multipliers/ws)
from app.deriv import market_proxy  # noqa: E402,F401  (registers WS /live/market)

__all__ = ["router"]
