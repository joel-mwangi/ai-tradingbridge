"""MT5 Phase 1 — market-order execution routes.

Kept in a dedicated module so mt5_bridge.py (market selection) is not changed.
All routes are mounted under the same /mt5 prefix via the shared router in main.py.
Authentication follows the same patterns as mt5_bridge.py:
  - User-facing routes use the Supabase JWT via current_user().
  - EA-facing routes use the raw device bearer credential via _ea_device().
Database calls go through Supabase REST + RPC, never directly to PostgreSQL.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictStr

from app.deriv.security import current_user
from app.mt5_bridge import _device_from_authorization, _request, _rpc, _IDEMPOTENCY_RE

router = APIRouter(prefix="/mt5", tags=["mt5-trade-orders"])

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
_BROKER_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_\.]{2,40}$")
_MAX_VOLUME = 100.0   # hard cap; broker limits are enforced by the EA itself
_MAX_SL_TP = 1_000_000.0


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CreateTradeOrderRequest(BaseModel):
    """Parameters for a Phase 1 market order (buy or sell at market price)."""
    broker_symbol: StrictStr = Field(
        min_length=2, max_length=40,
        description="Broker-side symbol (e.g. 'EURUSD', not the provider symbol).",
    )
    order_side: StrictStr = Field(
        description="Direction: 'buy' or 'sell'.",
    )
    volume: float = Field(
        gt=0, le=_MAX_VOLUME,
        description="Trade volume in lots.",
    )
    stop_loss: float | None = Field(
        default=None, gt=0, le=_MAX_SL_TP,
        description="Absolute price level for the stop loss; null = no stop loss.",
    )
    take_profit: float | None = Field(
        default=None, gt=0, le=_MAX_SL_TP,
        description="Absolute price level for the take profit; null = no take profit.",
    )
    deviation_points: int | None = Field(
        default=None, ge=0, le=10_000,
        description="Maximum acceptable slippage in points; null = broker default.",
    )
    magic_number: int = Field(
        default=0, ge=0, le=2_147_483_647,
        description="EA magic number for filtering; 0 = no filter.",
    )
    account_type: StrictStr = Field(
        description="'demo' or 'real'. The EA validates this against the connected account.",
    )
    idempotency_key: StrictStr = Field(
        min_length=8, max_length=128,
        description="Client-generated unique key; identical key + symbol = same order.",
    )
    expires_in_seconds: int = Field(
        default=60, ge=10, le=300,
        description="Seconds before the order expires if not picked up by the EA.",
    )


class EaSubmittedAck(BaseModel):
    """EA reports that OrderSend was called."""
    lease_token: UUID
    mt5_order_ticket: int | None = None
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)


class EaAcceptedAck(BaseModel):
    """EA reports that the broker accepted the order (ticket confirmed)."""
    lease_token: UUID
    mt5_order_ticket: int
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)


class EaRejectedAck(BaseModel):
    """EA reports that the broker or EA itself rejected the order."""
    lease_token: UUID
    mt5_order_ticket: int | None = None
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)
    reject_reason: StrictStr = Field(max_length=300)


class EaFilledAck(BaseModel):
    """EA reports a confirmed fill (position opened)."""
    lease_token: UUID
    mt5_order_ticket: int | None = None
    mt5_deal_ticket: int | None = None
    fill_price: float
    fill_volume: float
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)


class EaReconciledAck(BaseModel):
    """EA resolved an uncertain order by querying MT5 positions directly."""
    lease_token: UUID
    mt5_order_ticket: int | None = None
    mt5_deal_ticket: int | None = None
    fill_price: float | None = None
    fill_volume: float | None = None
    reject_reason: StrictStr | None = Field(default=None, max_length=300)
    mt5_retcode: int | None = None
    mt5_retcode_description: StrictStr | None = Field(default=None, max_length=300)


# ---------------------------------------------------------------------------
# User-facing routes
# ---------------------------------------------------------------------------

@router.post(
    "/devices/{device_id}/trade-orders",
    status_code=status.HTTP_201_CREATED,
)
async def create_trade_order(
    device_id: UUID,
    body: CreateTradeOrderRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Queue a Phase 1 market order for the named device."""

    # Validate enum fields not expressible in Pydantic alone
    if not _BROKER_SYMBOL_RE.fullmatch(body.broker_symbol):
        raise HTTPException(status_code=422, detail="Invalid broker symbol format.")
    if body.order_side not in ("buy", "sell"):
        raise HTTPException(status_code=422, detail="order_side must be 'buy' or 'sell'.")
    if body.account_type not in ("demo", "real"):
        raise HTTPException(status_code=422, detail="account_type must be 'demo' or 'real'.")
    if not _IDEMPOTENCY_RE.fullmatch(body.idempotency_key):
        raise HTTPException(status_code=422, detail="Invalid idempotency key format.")

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

    # Idempotency check
    existing = await _request(
        "GET", "mt5_trade_orders",
        params={
            "select": "id,broker_symbol,order_side,volume,status,created_at",
            "device_id": f"eq.{device_id}",
            "idempotency_key": f"eq.{body.idempotency_key}",
            "limit": "1",
        },
    )
    if isinstance(existing, list) and existing:
        previous = existing[0]
        # Idempotency: same key + same params → return existing order
        if (
            previous.get("broker_symbol") != body.broker_symbol
            or previous.get("order_side") != body.order_side
        ):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key already used for a different order.",
            )
        return {"order": previous, "duplicate": True}

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=body.expires_in_seconds)

    payload: dict[str, Any] = {
        "device_id": str(device_id),
        "user_id": user_id,
        "broker_symbol": body.broker_symbol,
        "order_side": body.order_side,
        "volume": body.volume,
        "stop_loss": body.stop_loss,
        "take_profit": body.take_profit,
        "deviation_points": body.deviation_points,
        "magic_number": body.magic_number,
        "account_type": body.account_type,
        "idempotency_key": body.idempotency_key,
        "status": "queued",
        "requested_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    rows = await _request(
        "POST", "mt5_trade_orders",
        payload=payload,
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=502, detail="Trade order could not be created.")
    return {"order": rows[0], "duplicate": False}


@router.get("/devices/{device_id}/trade-orders/{order_id}")
async def get_trade_order(
    device_id: UUID,
    order_id: UUID,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Poll for the current status and fill details of a trade order."""
    rows = await _request(
        "GET", "mt5_trade_orders",
        params={
            "select": (
                "id,broker_symbol,order_side,volume,stop_loss,take_profit,"
                "account_type,status,mt5_order_ticket,mt5_deal_ticket,"
                "fill_price,fill_volume,fill_time,mt5_retcode,"
                "mt5_retcode_description,reject_reason,"
                "requested_at,submitted_at,accepted_at,filled_at,"
                "reconciled_at,expired_at,cancelled_at,created_at"
            ),
            "id": f"eq.{order_id}",
            "device_id": f"eq.{device_id}",
            "user_id": f"eq.{user_id}",
            "limit": "1",
        },
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="Trade order not found.")
    return {"order": rows[0]}


@router.post(
    "/devices/{device_id}/trade-orders/{order_id}/cancel",
    status_code=status.HTTP_200_OK,
)
async def cancel_trade_order(
    device_id: UUID,
    order_id: UUID,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Cancel a queued order before the EA picks it up."""
    result = await _rpc("mt5_cancel_trade_order", {
        "p_user_id": user_id,
        "p_order_id": str(order_id),
    })
    record = result if isinstance(result, dict) else (
        result[0] if isinstance(result, list) and result else None
    )
    if not isinstance(record, dict) or not record.get("id"):
        raise HTTPException(
            status_code=409,
            detail="Order cannot be cancelled. It may already be leased or completed.",
        )
    return {"order": record}


# ---------------------------------------------------------------------------
# EA-facing routes
# ---------------------------------------------------------------------------

@router.get("/ea/trade-orders/next")
async def lease_next_trade_order(
    authorization: Annotated[str | None, Header()] = None,
    lease_seconds: int = Query(default=30, ge=5, le=120),
) -> dict[str, Any]:
    """EA polls for the next queued trade order for its device.

    Returns null when no order is waiting.
    The lease keeps the order locked for the EA for `lease_seconds` seconds.
    If the EA does not ack within that window, the order returns to 'queued'.
    """
    device = await _device_from_authorization(authorization)
    result = await _rpc("mt5_lease_next_trade_order", {
        "p_device_id": device["id"],
        "p_lease_seconds": lease_seconds,
    })
    order = result[0] if isinstance(result, list) and result else result
    if not isinstance(order, dict) or not order.get("id"):
        return {"order": None}

    # Return only the fields the EA needs to execute the trade.
    # Internal audit fields are omitted from the EA response.
    return {
        "order": {
            "id": order["id"],
            "broker_symbol": order["broker_symbol"],
            "order_side": order["order_side"],
            "volume": order["volume"],
            "stop_loss": order["stop_loss"],
            "take_profit": order["take_profit"],
            "deviation_points": order["deviation_points"],
            "magic_number": order["magic_number"],
            "account_type": order["account_type"],
            "expires_at": order["expires_at"],
            "lease_token": order["lease_token"],
        }
    }


def _call_ack_rpc(device_id: str, order_id: str, transition: str, **kwargs: Any):
    """Build the mt5_ack_trade_order RPC payload, omitting None values."""
    payload: dict[str, Any] = {
        "p_device_id": device_id,
        "p_order_id": order_id,
        "p_transition": transition,
    }
    for key, value in kwargs.items():
        if value is not None:
            payload[key] = value
    return payload


async def _apply_ack(
    authorization: str | None,
    order_id: UUID,
    transition: str,
    rpc_kwargs: dict[str, Any],
) -> dict[str, Any]:
    device = await _device_from_authorization(authorization)
    result = await _rpc("mt5_ack_trade_order",
                        _call_ack_rpc(device["id"], str(order_id),
                                      transition, **rpc_kwargs))
    record = result if isinstance(result, dict) else (
        result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else None
    )
    if not isinstance(record, dict):
        raise HTTPException(status_code=502, detail="Ack returned an invalid response.")
    return {
        "id": record.get("id"),
        "status": record.get("status"),
        "mt5_order_ticket": record.get("mt5_order_ticket"),
        "mt5_deal_ticket": record.get("mt5_deal_ticket"),
    }


@router.post("/ea/trade-orders/{order_id}/ack/submitted")
async def ack_submitted(
    order_id: UUID,
    body: EaSubmittedAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """EA has called OrderSend. Transition: leased → submitted."""
    return await _apply_ack(
        authorization, order_id, "submitted",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_order_ticket": body.mt5_order_ticket,
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
        },
    )


@router.post("/ea/trade-orders/{order_id}/ack/accepted")
async def ack_accepted(
    order_id: UUID,
    body: EaAcceptedAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Broker accepted the order; ticket confirmed. Transition: submitted → accepted."""
    return await _apply_ack(
        authorization, order_id, "accepted",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_order_ticket": body.mt5_order_ticket,
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
        },
    )


@router.post("/ea/trade-orders/{order_id}/ack/rejected")
async def ack_rejected(
    order_id: UUID,
    body: EaRejectedAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Broker or EA rejected the order. Transition: leased|submitted → rejected."""
    return await _apply_ack(
        authorization, order_id, "rejected",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_order_ticket": body.mt5_order_ticket,
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
            "p_reject_reason": body.reject_reason,
        },
    )


@router.post("/ea/trade-orders/{order_id}/ack/filled")
async def ack_filled(
    order_id: UUID,
    body: EaFilledAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """EA confirms position opened. Transition: accepted → filled."""
    return await _apply_ack(
        authorization, order_id, "filled",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_order_ticket": body.mt5_order_ticket,
            "p_mt5_deal_ticket": body.mt5_deal_ticket,
            "p_fill_price": body.fill_price,
            "p_fill_volume": body.fill_volume,
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
        },
    )


@router.post("/ea/trade-orders/{order_id}/ack/reconciled")
async def ack_reconciled(
    order_id: UUID,
    body: EaReconciledAck,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """EA reconciled an uncertain order by querying MT5. Transition: accepted|submitted → reconciled."""
    return await _apply_ack(
        authorization, order_id, "reconciled",
        {
            "p_lease_token": str(body.lease_token),
            "p_mt5_order_ticket": body.mt5_order_ticket,
            "p_mt5_deal_ticket": body.mt5_deal_ticket,
            "p_fill_price": body.fill_price,
            "p_fill_volume": body.fill_volume,
            "p_reject_reason": body.reject_reason,
            "p_mt5_retcode": body.mt5_retcode,
            "p_mt5_retcode_description": body.mt5_retcode_description,
        },
    )


__all__ = ["router"]
