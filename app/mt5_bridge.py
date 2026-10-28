"""Authenticated MT5 EA pairing and market-selection command API.

This bridge is configuration-only: it never places, modifies, or closes orders.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictStr

from app.deriv.security import current_user

router = APIRouter(prefix="/mt5", tags=["mt5-market-bridge"])
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class PairingCodeRequest(BaseModel):
    expires_in_seconds: int = Field(default=300, ge=60, le=600)


class PairDeviceRequest(BaseModel):
    code: StrictStr = Field(min_length=20, max_length=128)
    device_name: StrictStr = Field(min_length=1, max_length=100)


class CreateCommandRequest(BaseModel):
    provider_symbol: StrictStr = Field(min_length=2, max_length=32)
    idempotency_key: StrictStr = Field(min_length=8, max_length=128)
    expires_in_seconds: int = Field(default=120, ge=15, le=600)


class AckRequest(BaseModel):
    outcome: StrictStr
    result_code: StrictStr | None = Field(default=None, max_length=80)
    result_message: StrictStr | None = Field(default=None, max_length=500)


def _db_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise HTTPException(status_code=503, detail="MT5 command storage is not configured.")
    return url, key


def _headers(key: str, *, prefer: str | None = None) -> dict[str, str]:
    result = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        result["Prefer"] = prefer
    return result


async def _request(method: str, path: str, *, params: dict | None = None,
                   payload: Any = None, prefer: str | None = None) -> Any:
    url, key = _db_config()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.request(
                method, f"{url}/rest/v1/{path}", params=params,
                headers=_headers(key, prefer=prefer), json=payload,
            )
        if not response.is_success:
            # Keep database internals and credentials out of API responses.
            raise HTTPException(status_code=503, detail="MT5 command storage request failed.")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="MT5 command storage is unavailable.")


async def _rpc(name: str, payload: dict[str, Any]) -> Any:
    url, key = _db_config()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{url}/rest/v1/rpc/{name}",
                headers=_headers(key),
                json=payload,
            )
        if not response.is_success:
            # Avoid leaking SQL exceptions or function internals.
            code = response.status_code
            if code in (400, 401, 403, 404, 409):
                raise HTTPException(status_code=409, detail="MT5 command operation was rejected.")
            raise HTTPException(status_code=503, detail="MT5 command storage operation failed.")
        if not response.content:
            return None
        return response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="MT5 command storage is unavailable.")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _device_from_authorization(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="MT5 device authentication is required.")
    raw = authorization[7:].strip()
    if len(raw) < 32 or len(raw) > 256:
        raise HTTPException(status_code=401, detail="Invalid MT5 device credential.")
    rows = await _request(
        "GET", "mt5_devices",
        params={"select": "id,user_id,device_name,status", "credential_hash": f"eq.{_digest(raw)}", "limit": "1"},
    )
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise HTTPException(status_code=401, detail="Invalid MT5 device credential.")
    device = rows[0]
    if device.get("status") != "paired":
        raise HTTPException(status_code=403, detail="MT5 device is revoked.")
    await _request("PATCH", "mt5_devices", params={"id": f"eq.{device['id']}", "status": "eq.paired"},
                   payload={"last_seen_at": datetime.now(timezone.utc).isoformat(),
                            "updated_at": datetime.now(timezone.utc).isoformat()})
    return device


@router.post("/pairing-codes")
async def create_pairing_code(
    body: PairingCodeRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Create a short-lived single-use pairing code for the signed-in user."""
    code = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in_seconds)
    await _request(
        "POST", "mt5_pairing_codes",
        payload={"user_id": user_id, "code_hash": _digest(code), "expires_at": expires_at.isoformat()},
        prefer="return=minimal",
    )
    return {"code": code, "expires_at": expires_at.isoformat(), "one_time": True}


@router.post("/pair")
async def pair_device(body: PairDeviceRequest) -> dict[str, Any]:
    """Pair an EA using a one-time code; raw device credentials are returned only once."""
    if not body.device_name.strip():
        raise HTTPException(status_code=422, detail="Device name must not be blank.")
    credential = secrets.token_urlsafe(48)
    result = await _rpc("mt5_pair_device", {
        "p_code_hash": _digest(body.code),
        "p_device_name": body.device_name.strip(),
        "p_credential_hash": _digest(credential),
    })
    record = result[0] if isinstance(result, list) and result else result
    if not isinstance(record, dict) or not record.get("device_id"):
        raise HTTPException(status_code=502, detail="MT5 pairing returned an invalid response.")
    return {"device_id": record["device_id"], "device_credential": credential, "credential_shown_once": True}


@router.get("/devices")
async def list_devices(user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    rows = await _request(
        "GET", "mt5_devices",
        params={"select": "id,device_name,status,last_seen_at,created_at", "user_id": f"eq.{user_id}",
                "order": "created_at.desc"},
    )
    return {"devices": rows if isinstance(rows, list) else []}


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(device_id: UUID, user_id: Annotated[str, Depends(current_user)]) -> None:
    rows = await _request(
        "PATCH", "mt5_devices",
        params={"id": f"eq.{device_id}", "user_id": f"eq.{user_id}", "status": "eq.paired"},
        payload={"status": "revoked", "updated_at": datetime.now(timezone.utc).isoformat()},
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="Paired MT5 device not found.")


@router.post("/devices/{device_id}/commands", status_code=status.HTTP_201_CREATED)
async def create_market_command(
    device_id: UUID,
    body: CreateCommandRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    if not _SYMBOL_RE.fullmatch(body.provider_symbol):
        raise HTTPException(status_code=422, detail="Invalid provider symbol format.")
    if not _IDEMPOTENCY_RE.fullmatch(body.idempotency_key):
        raise HTTPException(status_code=422, detail="Invalid idempotency key.")
    devices = await _request(
        "GET", "mt5_devices",
        params={"select": "id,status", "id": f"eq.{device_id}", "user_id": f"eq.{user_id}", "limit": "1"},
    )
    if not isinstance(devices, list) or not devices or devices[0].get("status") != "paired":
        raise HTTPException(status_code=404, detail="Paired MT5 device not found.")
    existing = await _request(
        "GET", "mt5_commands",
        params={"select": "id,provider_symbol,status,expires_at", "device_id": f"eq.{device_id}",
                "idempotency_key": f"eq.{body.idempotency_key}", "limit": "1"},
    )
    if isinstance(existing, list) and existing:
        previous = existing[0]
        if previous.get("provider_symbol") != body.provider_symbol:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for a different market.")
        return {"command": previous, "duplicate": True}
    now = datetime.now(timezone.utc)
    payload = {
        "device_id": str(device_id), "user_id": user_id, "command_type": "select_market",
        "provider_symbol": body.provider_symbol, "idempotency_key": body.idempotency_key,
        "status": "pending", "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=body.expires_in_seconds)).isoformat(),
    }
    rows = await _request("POST", "mt5_commands", payload=payload, prefer="return=representation")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=502, detail="Market command could not be created.")
    return {"command": rows[0], "duplicate": False}


@router.get("/ea/commands/next")
async def lease_next_command(
    authorization: Annotated[str | None, Header()] = None,
    lease_seconds: int = Query(default=20, ge=5, le=60),
) -> dict[str, Any]:
    """EA-only endpoint; requires the paired device credential."""
    device = await _device_from_authorization(authorization)
    result = await _rpc("mt5_lease_next_command", {
        "p_device_id": device["id"], "p_lease_seconds": lease_seconds,
    })
    command = result[0] if isinstance(result, list) and result else result
    if not isinstance(command, dict) or not command.get("id"):
        return {"command": None}
    return {"command": {
        "id": command["id"], "type": command["command_type"],
        "provider_symbol": command["provider_symbol"], "expires_at": command["expires_at"],
    }}


@router.post("/ea/commands/{command_id}/ack")
async def acknowledge_command(
    command_id: UUID,
    body: AckRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    device = await _device_from_authorization(authorization)
    if body.outcome not in ("acknowledged", "rejected"):
        raise HTTPException(status_code=422, detail="Outcome must be acknowledged or rejected.")
    result = await _rpc("mt5_ack_command", {
        "p_device_id": device["id"], "p_command_id": str(command_id),
        "p_outcome": body.outcome, "p_result_code": body.result_code,
        "p_result_message": body.result_message,
    })
    record = result if isinstance(result, dict) else (
        result[0] if isinstance(result, list) and result and isinstance(result[0], dict) else None
    )
    if not record:
        raise HTTPException(status_code=502, detail="MT5 acknowledgement returned an invalid response.")
    return {"id": record.get("id"), "status": record.get("status")}


__all__ = ["router"]
