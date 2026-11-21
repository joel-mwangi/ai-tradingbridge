"""Authenticated API for bounded, user-controlled live tick collection jobs."""
from __future__ import annotations

import hmac
import os
from typing import Annotated
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StrictInt

from app.even_odd.storage import _configuration, _headers
from app.even_odd.collector import normalize_live_tick

router = APIRouter(prefix="/even-odd/collection-jobs", tags=["even-odd-lab"])
bearer = HTTPBearer(auto_error=False)
ALLOWED_TARGETS = {100, 500, 1000, 2500, 5000}


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
        raise HTTPException(status_code=422, detail="target_ticks must be 100, 500, 1000, 2500, or 5000.")
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
        if created.status_code == 409:
            raise HTTPException(status_code=409, detail="A collection is already active for this market.")
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
        job = rows[0] if rows else None
        if job and job.get("status") in {"queued", "collecting"}:
            timestamp = job.get("updated_at") or job.get("created_at")
            if timestamp:
                try:
                    last_seen = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    stale = (datetime.now(timezone.utc) - last_seen).total_seconds() > 120
                except ValueError:
                    stale = False
                if stale:
                    recovered = await client.patch(
                        f"{url}/rest/v1/even_odd_collection_jobs",
                        params={"id": f"eq.{job['id']}", "owner_user_id": f"eq.{owner}",
                                "status": "in.(queued,collecting)"},
                        headers=_headers(key, "return=representation"),
                        json={"status": "failed", "last_error": "Browser heartbeat expired; collection was interrupted.",
                              "completed_at": datetime.now(timezone.utc).isoformat()},
                    )
                    recovered.raise_for_status()
                    recovered_rows = recovered.json()
                    if recovered_rows:
                        job = recovered_rows[0]
        return {"job": job, "live_only": True}


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
            json={"status": "stopped", "completed_at": datetime.now(timezone.utc).isoformat()},
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise HTTPException(status_code=404, detail="Active collection job not found.")
        return {"job": rows[0], "stop_requested": True, "order_execution_enabled": False}


class LiveTickBatchRequest(BaseModel):
    batch_id: UUID
    ticks: list[dict] = Field(min_length=1, max_length=100)


@router.post("/{job_id}/batch")
async def persist_browser_tick_batch(
    job_id: UUID,
    request: LiveTickBatchRequest,
    owner: Annotated[UUID, Depends(_auth)],
) -> dict:
    """Atomically persist one idempotent browser-captured batch and job progress."""
    url, key = await _client()
    normalized_ticks: list[dict] = []
    # Read the owner-scoped job only to validate the incoming Deriv payload's
    # symbol. The database RPC repeats ownership/status/target checks under a
    # row lock and is the sole authority for progress updates.
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        response = await client.get(
            f"{url}/rest/v1/even_odd_collection_jobs",
            params={"select": "id,market_symbol", "id": f"eq.{job_id}",
                    "owner_user_id": f"eq.{owner}", "limit": "1"},
            headers=_headers(key),
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise HTTPException(status_code=404, detail="Collection job not found.")
        symbol = str(rows[0]["market_symbol"])
        try:
            for item in request.ticks:
                raw_tick = item.get("tick")
                subscription = item.get("subscription_id")
                normalized_ticks.append(
                    normalize_live_tick(
                        raw_tick,
                        symbol,
                        subscription_id=str(subscription) if subscription is not None else None,
                    )
                )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Batch contains an invalid Deriv tick.") from exc

        rpc = await client.post(
            f"{url}/rest/v1/rpc/even_odd_persist_browser_tick_batch",
            headers=_headers(key),
            json={
                "p_job_id": str(job_id),
                "p_owner_user_id": str(owner),
                "p_batch_id": str(request.batch_id),
                "p_ticks": normalized_ticks,
            },
        )
        if rpc.status_code in {404, 409}:
            raise HTTPException(status_code=409, detail="Collection job is no longer active or batch is invalid.")
        if rpc.status_code == 400:
            raise HTTPException(status_code=422, detail="Database rejected the tick batch.")
        try:
            rpc.raise_for_status()
            result = rpc.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Could not atomically persist the live tick batch.") from exc
        if isinstance(result, dict) and result.get("error"):
            detail = str(result["error"])
            if "not found" in detail:
                raise HTTPException(status_code=404, detail=detail)
            if "no longer active" in detail or "already used" in detail or "exceed" in detail:
                raise HTTPException(status_code=409, detail=detail)
            raise HTTPException(status_code=422, detail=detail)
        if not isinstance(result, dict) or not isinstance(result.get("job"), dict):
            raise HTTPException(status_code=502, detail="Database returned an invalid batch receipt.")
        return result

@router.post("/{job_id}/heartbeat")
async def collection_heartbeat(
    job_id: UUID,
    owner: Annotated[UUID, Depends(_auth)],
) -> dict:
    """Record browser liveness so abandoned jobs can be recovered safely."""
    url, key = await _client()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        response = await client.patch(
            f"{url}/rest/v1/even_odd_collection_jobs",
            params={"id": f"eq.{job_id}", "owner_user_id": f"eq.{owner}", "status": "in.(queued,collecting)"},
            headers=_headers(key, "return=representation"),
            json={"status": "collecting", "updated_at": datetime.now(timezone.utc).isoformat()},
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise HTTPException(status_code=409, detail="Collection job is no longer active.")
        return {"job": rows[0], "heartbeat": True}
