"""Authenticated control-plane API for durable Demo bot runs.

The API only persists run commands. A separately deployed, always-on worker must consume
queued runs; the HTTP service does not launch trades inside request handlers.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.deriv_gateway import current_user

router = APIRouter(prefix="/bots", tags=["bots"])
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9]{2,32}$")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")
CONTRACTS = {"DIGITOVER", "DIGITUNDER", "DIGITEVEN", "DIGITODD", "DIGITMATCH", "DIGITDIFF"}


def _db_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Bot storage is not configured.")
    return url, key


async def _db_request(method: str, table: str, *, params: dict[str, str] | None = None,
                      payload: Any = None, prefer: str | None = None) -> Any:
    url, key = _db_config()
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.request(method, f"{url}/rest/v1/{table}", params=params,
                                            headers=headers, json=payload)
        if response.status_code == 409:
            raise HTTPException(status_code=409, detail="A run already exists for this bot and account.")
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Bot storage request failed.")
        if not response.content:
            return None
        return response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Bot storage is unavailable.")


async def _require_live_worker() -> None:
    """Refuse to queue a run unless the persistent worker has a fresh heartbeat."""
    rows = await _db_request("GET", "bot_worker_heartbeats", params={
        "select": "last_seen_at", "order": "last_seen_at.desc", "limit": "1",
    })
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=503, detail="Automated execution worker is offline; no run was queued.")
    try:
        seen = datetime.fromisoformat(str(rows[0]["last_seen_at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - seen).total_seconds()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Worker health could not be verified; no run was queued.")
    if age < -5 or age > 45:
        raise HTTPException(status_code=503, detail="Automated execution worker is offline or stale; no run was queued.")


class FixedStakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern="^fixed$")
    amount: float = Field(gt=0, le=1000)


class StopCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern="^(profit|loss|tradeCount|time)$")
    target: float | None = Field(default=None, gt=0)
    limit: float | None = Field(default=None, gt=0)
    max: int | None = Field(default=None, ge=1, le=20)
    seconds: int | None = Field(default=None, ge=1, le=1800)


class DemoBotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr = Field(min_length=1, max_length=80)
    symbol: StrictStr = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")
    contractType: StrictStr
    barrier: str | None = Field(default=None, pattern=r"^\d$")
    duration: int = Field(ge=1, le=10)
    durationUnit: str = Field(pattern="^t$")
    stakeStrategy: FixedStakeConfig
    stopConditions: list[StopCondition] = Field(min_length=1, max_length=4)
    cooldownSeconds: int = Field(default=2, ge=1, le=60)
    maxTradesPerDay: int = Field(default=20, ge=1, le=20)
    maxConsecutiveLosses: int = Field(default=3, ge=1, le=3)

    def validate_contract(self) -> None:
        if self.contractType not in CONTRACTS:
            raise HTTPException(status_code=422, detail="Unsupported digit contract.")
        needs_barrier = self.contractType in {"DIGITOVER", "DIGITUNDER", "DIGITMATCH", "DIGITDIFF"}
        if needs_barrier != (self.barrier is not None):
            raise HTTPException(status_code=422, detail="Barrier does not match contract type.")
        if self.contractType == "DIGITOVER" and int(self.barrier or "0") > 8:
            raise HTTPException(status_code=422, detail="Over barrier must be 0–8.")
        if self.contractType == "DIGITUNDER" and int(self.barrier or "9") < 1:
            raise HTTPException(status_code=422, detail="Under barrier must be 1–9.")
        for stop in self.stopConditions:
            required = {"profit": "target", "loss": "limit", "tradeCount": "max", "time": "seconds"}[stop.type]
            if getattr(stop, required) is None:
                raise HTTPException(status_code=422, detail=f"Stop condition {stop.type} requires {required}.")
        if not any(s.type in {"loss", "tradeCount", "time"} for s in self.stopConditions):
            raise HTTPException(status_code=422, detail="At least one loss, trade-count, or time limit is required.")


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: StrictStr = Field(min_length=2, max_length=32)
    config: DemoBotConfig


@router.post("/runs", status_code=202)
async def create_run(body: CreateRunRequest, user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    """Queue a constrained Demo run. This endpoint never executes trades inline."""
    if not ACCOUNT_ID_RE.fullmatch(body.account_id):
        raise HTTPException(status_code=422, detail="Invalid account identifier.")
    body.config.validate_contract()
    await _require_live_worker()
    # Fail closed when the persistent worker is not actually online; do not create queued runs that can never execute.
    live_after = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()
    workers = await _db_request("GET", "bot_worker_heartbeats", params={
        "select": "worker_id,last_seen_at", "last_seen_at": f"gt.{live_after}",
        "order": "last_seen_at.desc", "limit": "1",
    })
    if not isinstance(workers, list) or not workers:
        raise HTTPException(status_code=503, detail="Automated execution worker is offline; no run was queued.")
    # Verify the account is a linked Demo account under this user's Deriv OAuth identity.
    from app.deriv_gateway import _linked_account_ids, _owned_credential
    credential = await _owned_credential(user_id)
    if credential is None:
        raise HTTPException(status_code=401, detail="Connect your Deriv account before starting a bot.")
    linked = await _linked_account_ids(user_id, [body.account_id])
    if body.account_id not in linked:
        raise HTTPException(status_code=403, detail="Trading account is not linked to this user.")
    # Check the account type using Deriv's authenticated account listing; fail closed.
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
            )
        if not response.is_success:
            raise HTTPException(status_code=502, detail="Could not verify the Deriv account.")
        payload = response.json()
        root = payload if isinstance(payload, dict) else {}
        data = root.get("data")
        accounts = data if isinstance(data, list) else data.get("accounts", []) if isinstance(data, dict) else root.get("accounts", [])
        account = next((a for a in accounts if isinstance(a, dict) and a.get("account_id") == body.account_id), None)
        if not account or str(account.get("account_type", "")).lower() != "demo":
            raise HTTPException(status_code=403, detail="Automated execution is Demo-only.")
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Could not verify the Deriv account.")

    run = {
        "user_id": user_id, "account_id": body.account_id, "bot_id": body.config.id,
        "config": body.config.model_dump(), "status": "queued", "currency": str(account.get("currency") or "USD"),
    }
    rows = await _db_request("POST", "bot_runs", params={"select": "*"}, payload=run, prefer="return=representation")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=503, detail="Run was not persisted; no trading was started.")
    return {"run": rows[0], "execution": "queued", "note": "A persistent worker must be online to execute queued runs."}


@router.get("/runs")
async def list_runs(user_id: Annotated[str, Depends(current_user)], limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    rows = await _db_request("GET", "bot_runs", params={
        "select": "*", "user_id": f"eq.{user_id}", "order": "created_at.desc", "limit": str(limit),
    })
    return {"runs": rows if isinstance(rows, list) else []}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    if len(run_id) > 64:
        raise HTTPException(status_code=404, detail="Run not found.")
    rows = await _db_request("GET", "bot_runs", params={
        "select": "*", "id": f"eq.{run_id}", "user_id": f"eq.{user_id}", "limit": "1",
    })
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="Run not found.")
    trades = await _db_request("GET", "bot_trades", params={
        "select": "*", "run_id": f"eq.{run_id}", "user_id": f"eq.{user_id}", "order": "created_at.desc",
    })
    return {"run": rows[0], "trades": trades if isinstance(trades, list) else []}


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    rows = await _db_request("GET", "bot_runs", params={
        "select": "id,status", "id": f"eq.{run_id}", "user_id": f"eq.{user_id}", "limit": "1",
    })
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="Run not found.")
    current_status = rows[0].get("status")
    # An ambiguous purchase is a safety lock, not a state the user can clear by
    # pressing Stop. Broker reconciliation is required before another run.
    if current_status == "reconciliation_required":
        return {"run_id": run_id, "status": current_status, "changed": False,
                "note": "Reconcile the unresolved Deriv contract before clearing this safety lock."}
    if current_status == "queued":
        updated = await _db_request("PATCH", "bot_runs", params={
            "id": f"eq.{run_id}", "user_id": f"eq.{user_id}", "status": "eq.queued", "select": "id,status",
        }, payload={"status": "stopped", "ended_at": datetime.now(timezone.utc).isoformat(),
                    "stop_reason": "User stopped the run before execution."}, prefer="return=representation")
        if not isinstance(updated, list) or not updated:
            raise HTTPException(status_code=409, detail="Run state changed concurrently; refresh before retrying.")
        return {"run_id": run_id, "status": "stopped", "changed": True}
    if current_status != "running":
        return {"run_id": run_id, "status": current_status, "changed": False}
    updated = await _db_request("PATCH", "bot_runs", params={
        "id": f"eq.{run_id}", "user_id": f"eq.{user_id}", "status": "eq.running", "select": "id,status",
    }, payload={"status": "stopping", "stop_reason": "User requested stop."}, prefer="return=representation")
    if not isinstance(updated, list) or not updated:
        raise HTTPException(status_code=409, detail="Run state changed concurrently; refresh before retrying.")
    return {"run_id": run_id, "status": "stopping", "changed": True}
