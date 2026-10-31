"""MT5 Phase 2 — position close / modify command routes.

Kept in a dedicated module, separate from mt5_trade_orders.py, so the
existing trade-order queue is never disturbed.  All routes are mounted under
the same /mt5 prefix via the shared router registered in main.py.

Authentication mirrors mt5_trade_orders.py:
  - User-facing routes: Supabase JWT via current_user().
  - EA-facing routes: raw device bearer credential via _device_from_authorization().
Database calls go through Supabase REST + RPC.
"""
from __future__ import annotations

import os
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictStr

from app.deriv.security import current_user
from app.mt5_bridge import _device_from_authorization, _request, _rpc, _IDEMPOTENCY_RE

router = APIRouter(prefix="/mt5", tags=["mt5-position-commands"])

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CreatePositionCommandRequest(BaseModel):
    """Parameters for a position close or modify command."""
    command_type: StrictStr = Field(
        description="'close' or 'modify'.",
    )
    mt5_position_ticket: int = Field(
        gt=0,
        description="MT5 ticket of the open position to act on.",
    )
    volume: float | None = Field(
        default=None, gt=0,
        description="Lots to close. Null = full close (only used for 'close' commands).",
    )
    new_stop_loss: float | None = Field(
        default=None, gt=0, le=1_000_000.0,
        description="New absolute stop-loss price level (only used for 'modify' commands).",
    )
    new_take_profit: float | None = Field(
        default=None, gt=0, le=1_000_000.0,
        description="New absolute take-profit price level (only used for 'modify' commands).",
    )
    account_type: StrictStr = Field(
        description="'demo' or 'real'. The EA validates this against the connected account.",
    )
    idempotency_key: StrictStr | None = Field(
        default=None, min_length=8, max_length=128,
        description="Optional client-generated unique key; prevents duplicate commands.",
    )
    expires_in_seconds: int = Field(
        default=60, ge=10, le=300,
        description="Seconds before the command expires if not picked up by the EA.",
    )


class EaPositionSubmittedAck(BaseModel):
    """EA reports that the position command was sent to MT5."""
    lease_token: UUID
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)


class EaPositionDoneAck(BaseModel):
    """EA reports that the position command completed successfully."""
    lease_token: UUID
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)


class EaPositionRejectedAck(BaseModel):
    """EA reports that the position command was rejected."""
    lease_token: UUID
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)
    reject_reason: StrictStr = Field(max_length=300)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _position_ack_rpc(device_id: str, cmd_id: str, transition: str, **kwargs: Any) -> dict[str, Any]:
    """Build mt5_ack_position_command payload, omitting None values."""
    payload: dict[str, Any] = {
        "p_device_id": device_id,
        "p_cmd_id": cmd_id,
        "p_transition": transition,
    }
    for key, value in kwargs.items():
        if value is not None:
            payload[key] = value
    return payload


async def _apply_position_ack(
    authorization: str | None,
    cmd_id: UUID,
    transition: str,
    rpc_kwargs: dict[str, Any],
) -> dict[str, Any]:
    device = await _device_from_authorization(authorization)
    result = await _rpc(
        "mt5_ack_position_command",
        _position_ack_rpc(device["id"], str(cmd_id), transition, **rpc_kwargs),
    )
    record = result if isinstance(result, dict) else (
        result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else None
    )
    if not isinstance(record, dict):
        raise HTTPException(status_code=502, detail="Ack returned an invalid response.")
    return {
        "id": record.get("id"),
        "status": record.get("status"),
    }


# ---------------------------------------------------------------------------
# User-facing routes
# ---------------------------------------------------------------------------

@router.post(
    "/devices/{device_id}/position-commands",
    status_code=status.HTTP_201_CREATED,
)
async def create_position_command(
    device_id: UUID,
    body: CreatePositionCommandRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Queue a position close or modify command for the named device."""

    # Validate enum fields
    if body.command_type not in ("close", "modify"):
        raise HTTPException(status_code=422, detail="command_type must be 'close' or 'modify'.")
    if body.account_type not in ("demo", "real"):
        raise HTTPException(status_code=422, detail="account_type must be 'demo' or 'real'.")
    if body.idempotency_key is not None and not _IDEMPOTENCY_RE.fullmatch(body.idempotency_key):
        raise HTTPException(status_code=422, detail="Invalid idempotency key format.")

    # Real-account partial-close volume cap
    if body.account_type == "real" and body.volume is not None:
        raw = os.getenv("TRADEBRIDGE_MT5_REAL_MAX_VOLUME", "").strip()
        if raw:
            try:
                cap = float(raw)
                if cap > 0 and body.volume > cap:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Real-account close volume {body.volume} exceeds the configured maximum of {cap} lots.",
                    )
            except ValueError:
                pass

    # Verify device belongs to this user and is paired
    devices = await _request(
        "GET", "mt5_devices",
        params={
            "select": "id,status",
            "id": f"eq.{device_id}",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    )
    if not isinstance(devices, list) or not devices or devices[0].get("status") != "paired":
        raise HTTPException(status_code=404, detail="Paired MT5 device not found.")

    payload: dict[str, Any] = {
        "device_id": str(device_id),
        "user_id": user_id,
        "command_type": body.command_type,
        "mt5_position_ticket": body.mt5_position_ticket,
        "volume": body.volume,
        "new_stop_loss": body.new_stop_loss,
        "new_take_profit": body.new_take_profit,
        "account_type": body.account_type,
        "idempotency_key": body.idempotency_key,
        "status": "queued",
    }

    rows = await _request(
        "POST", "mt5_position_commands",
        payload=payload,
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=502, detail="Position command could not be created.")
    return {"command": rows[0]}


@router.get("/devices/{device_id}/position-commands/{cmd_id}")
async def get_position_command(
    device_id: UUID,
    cmd_id: UUID,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Poll the current status of a position command."""
    rows = await _request(
        "GET", "mt5_position_commands",
        params={
            "select": "*",
            "id": f"eq.{cmd_id}",
            "device_id": f"eq.{device_id}",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="Position command not found.")
    return {"command": rows[0]}


@router.post(
    "/devices/{device_id}/position-commands/{cmd_id}/cancel",
    status_code=status.HTTP_200_OK,
)
async def cancel_position_command(
    device_id: UUID,
    cmd_id: UUID,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Cancel a queued position command before the EA picks it up."""
    result = await _rpc("mt5_cancel_position_command", {
        "p_user_id": user_id,
        "p_cmd_id": str(cmd_id),
    })
    record = result if isinstance(result, dict) else (
        result[0] if isinstance(result, list) and result else None
    )
    if not isinstance(record, dict) or not record.get("id"):
        raise HTTPException(
            status_code=409,
            detail="Command cannot be cancelled. It may already be leased or completed.",
        )
    return {"command": record}


# ---------------------------------------------------------------------------
# EA-facing routes
# ---------------------------------------------------------------------------

@router.get("/ea/position-commands/next")
async def lease_next_position_command(
    authorization: Annotated[str | None, Header()] = None,
    lease_seconds: int = Query(default=30, ge=5, le=120),
) -> dict[str, Any]:
    """EA polls for the next queued position command for its device.

    Returns null when no command is waiting.
    """
    device = await _device_from_authorization(authorization)
    result = await _rpc("mt5_lease_next_position_command", {
        "p_device_id": device["id"],
        "p_lease_seconds": lease_seconds,
    })
    cmd = result[0] if isinstance(result, list) and result else result
    if not isinstance(cmd, dict) or not cmd.get("id"):
        return {"command": None}

    return {
        "command": {
            "id": cmd["id"],
            "command_type": cmd["command_type"],
            "mt5_position_ticket": cmd["mt5_position_ticket"],
            "volume": cmd["volume"],
            "new_stop_loss": cmd["new_stop_loss"],
            "new_take_profit": cmd["new_take_profit"],
            "account_type": cmd["account_type"],
            "expires_at": cmd["expires_at"],
            "lease_token": cmd["lease_token"],
        }
    }


@router.post("/ea/position-commands/{cmd_id}/ack/submitted")
async def ack_position_submitted(
    cmd_id: UUID,
    body: EaPositionSubmittedAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """EA sent the command to MT5. Transition: leased → submitted."""
    return await _apply_position_ack(
        authorization, cmd_id, "submitted",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
        },
    )


@router.post("/ea/position-commands/{cmd_id}/ack/done")
async def ack_position_done(
    cmd_id: UUID,
    body: EaPositionDoneAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Position command completed successfully. Transition: submitted → done."""
    return await _apply_position_ack(
        authorization, cmd_id, "done",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
        },
    )


@router.post("/ea/position-commands/{cmd_id}/ack/rejected")
async def ack_position_rejected(
    cmd_id: UUID,
    body: EaPositionRejectedAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Position command rejected by MT5 or EA. Transition: submitted → rejected."""
    return await _apply_position_ack(
        authorization, cmd_id, "rejected",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
            "p_reject_reason": body.reject_reason,
        },
    )


__all__ = ["router"]
