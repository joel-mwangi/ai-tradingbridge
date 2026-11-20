"""Authenticated API for bounded, user-controlled live tick collection jobs."""
from __future__ import annotations

import hmac
import os
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StrictInt

from app.even_odd.storage import _configuration, _headers, EvenOddStorageError

router = APIRouter(prefix="/even-odd/collection-jobs", tags=["even-odd-lab"])
bearer = HTTPBearer(auto_error=False)
ALLOWED_TARGETS = {500, 1000, 2500, 5000}


def _auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    user_id: Annotated[str | None, Header(alias="X-TradeBridge-User-ID")] = None,
) -> UUID:
    expected = os.getenv("TRADEBRIDGE_API_TOKEN", "")
    supplied = credentials.credentials if credentials else ""
    if not expected:
        raise HTTPException(status_code=503, detail="API authentication is not configured.")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API token.")
    try:
        return UUID(user_id or "")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="Authenticated user context is required.") from exc


class StartCollectionRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$")
    target_ticks: StrictInt = Field(default=1000)


async def _client():
    url, key = _configuration()
    return url, key


@router.post("")
async def start_collection(
    request: StartCollectionRequest,
    owner: Annotated[UUID, Depends(_auth)],
) -> dict:
    if request.target_ticks not in ALLOWED_TARGETS:
        raise HTTPException(status_code=422, detail="target_ticks must be 500, 1000, 2500, or 5000.")
    url, key = await _client()
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Reject a second active job for the same owner and market.
        active = await client.get(
            f"{url}/rest/v1/even_odd_collection_jobs",
            params={"select": "id,status", "owner_user_id": f"eq.{owner}",
                    "market_symbol": f"eq.{request.symbol}",
                    "status": "in.(queued,collecting)", "limit": "1"},
            headers=_headers(key),
        )
        active.raise_for_status()
        if active.json():
            raise HTTPException(status_code=409, detail="An active collection already exists for this market.")
        created = await client.post(
            f"{url}/rest/v1/even_odd_collection_jobs",
            headers=_headers(key, "return=representation"),
            json={"owner_user_id": str(owner), "market_symbol": request.symbol,
                  "target_ticks": request.target_ticks, "status": "queued"},
        )
        created.raise_for_status()
        rows = created.json()
        if not isinstance(rows, list) or not rows:
            raise HTTPException(status_code=502, detail="Collection job was not created.")
        return {"job": rows[0], "live_only": True, "order_execution_enabled": False}


@router.get("/active")
async def active_collection(
    owner: Annotated[UUID, Depends(_auth)],
    symbol: str = Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$"),
) -> dict:
    url, key = await _client()
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        response = await client.get(
            f"{url}/rest/v1/even_odd_collection_jobs",
            params={"select": "*", "owner_user_id": f"eq.{owner}",
                    "market_symbol": f"eq.{symbol}",
                    "order": "created_at.desc", "limit": "1"},
            headers=_headers(key),
        )
        response.raise_for_status()
        rows = response.json()
        return {"job": rows[0] if rows else None, "live_only": True}


@router.post("/{job_id}/stop")
async def stop_collection(
    job_id: UUID,
    owner: Annotated[UUID, Depends(_auth)],
) -> dict:
    url, key = await _client()
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        response = await client.patch(
            f"{url}/rest/v1/even_odd_collection_jobs",
            params={"id": f"eq.{job_id}", "owner_user_id": f"eq.{owner}",
                    "status": "in.(queued,collecting)"},
            headers=_headers(key, "return=representation"),
            json={"status": "stopped"},
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise HTTPException(status_code=404, detail="Active collection job not found.")
        return {"job": rows[0], "stop_requested": True, "order_execution_enabled": False}
