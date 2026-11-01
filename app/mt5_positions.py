"""MT5 Phase 3 — real-time position sync routes.

Three endpoints:
  POST /mt5/ea/positions/sync           EA pushes batch of open positions (upsert)
  POST /mt5/ea/positions/{ticket}/closed EA reports a position closed
  GET  /mt5/devices/{device_id}/positions  UI polls open positions

Authentication:
  EA-facing routes use raw device bearer credential via _device_from_authorization.
  User-facing routes use Supabase JWT via current_user.
All DB writes use Supabase REST (_request), never direct PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, StrictStr

from app.deriv.security import current_user
from app.mt5_bridge import _device_from_authorization, _request

router = APIRouter(prefix="/mt5", tags=["mt5-positions"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PositionSnapshot(BaseModel):
    """One open position as reported by the MT5 EA."""
    mt5_ticket: int = Field(gt=0)
    symbol: StrictStr = Field(min_length=2, max_length=40)
    order_side: StrictStr  # 'buy' | 'sell'
    volume: float = Field(gt=0)
    open_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    floating_pl: float
    stop_loss: float | None = None
    take_profit: float | None = None
    account_type: StrictStr  # 'demo' | 'real'
    opened_at: str  # ISO-8601


class SyncPositionsRequest(BaseModel):
    positions: list[PositionSnapshot] = Field(max_length=500)


class PositionClosedRequest(BaseModel):
    closed_at: str | None = None  # ISO-8601; defaults to server now()


# ---------------------------------------------------------------------------
# EA-facing routes
# ---------------------------------------------------------------------------

@router.post("/ea/positions/sync", status_code=status.HTTP_200_OK)
async def sync_positions(
    body: SyncPositionsRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """EA pushes all currently open positions. Upsert by device_id + mt5_ticket."""
    device = await _device_from_authorization(authorization)
    device_id: str = device["id"]
    user_id: str = device["user_id"]
    now = datetime.now(timezone.utc).isoformat()

    upserted = 0
    for pos in body.positions:
        if pos.order_side not in ("buy", "sell"):
            continue
        if pos.account_type not in ("demo", "real"):
            continue
        row: dict[str, Any] = {
            "device_id": device_id,
            "user_id": user_id,
            "mt5_ticket": pos.mt5_ticket,
            "symbol": pos.symbol,
            "order_side": pos.order_side,
            "volume": pos.volume,
            "open_price": pos.open_price,
            "current_price": pos.current_price,
            "floating_pl": pos.floating_pl,
            "stop_loss": pos.stop_loss,
            "take_profit": pos.take_profit,
            "account_type": pos.account_type,
            "is_open": True,
            "opened_at": pos.opened_at,
            "closed_at": None,
            "last_synced_at": now,
        }
        await _request(
            "POST",
            "mt5_positions",
            payload=row,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        upserted += 1
    return {"upserted": upserted}


@router.post("/ea/positions/{ticket}/closed", status_code=status.HTTP_200_OK)
async def mark_position_closed(
    ticket: int,
    body: PositionClosedRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """EA reports that position `ticket` has closed."""
    device = await _device_from_authorization(authorization)
    device_id: str = device["id"]
    closed_at = body.closed_at or datetime.now(timezone.utc).isoformat()

    rows = await _request(
        "PATCH",
        "mt5_positions",
        payload={"is_open": False, "closed_at": closed_at},
        params={
            "device_id": f"eq.{device_id}",
            "mt5_ticket": f"eq.{ticket}",
            "is_open": "eq.true",
        },
        prefer="return=minimal",
    )
    return {"closed": True}


# ---------------------------------------------------------------------------
# User-facing route
# ---------------------------------------------------------------------------

@router.get("/devices/{device_id}/positions")
async def list_positions(
    device_id: UUID,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Return all currently open positions for the device."""
    # Verify device belongs to this user and is paired
    devices = await _request(
        "GET",
        "mt5_devices",
        params={
            "select": "id,status",
            "id": f"eq.{device_id}",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    )
    if not isinstance(devices, list) or not devices or devices[0].get("status") != "paired":
        raise HTTPException(status_code=404, detail="Paired MT5 device not found.")

    rows = await _request(
        "GET",
        "mt5_positions",
        params={
            "select": "*",
            "device_id": f"eq.{device_id}",
            "user_id": f"eq.{user_id}",
            "is_open": "eq.true",
            "order": "opened_at.asc",
        },
    )
    if not isinstance(rows, list):
        rows = []
    return {"positions": rows, "count": len(rows)}


__all__ = ["router"]
