"""Worker for user-triggered, bounded live Deriv tick collection jobs."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from datetime import datetime, timezone

import httpx
import websockets

from app.even_odd.collector import DERIV_PUBLIC_WS_URL, normalize_live_tick
from app.even_odd.storage import _configuration, _headers, persist_live_tick_batch

WORKER_ID = os.getenv("EVEN_ODD_WORKER_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
BATCH_SIZE = 100


async def _claim(client: httpx.AsyncClient, url: str, key: str) -> dict | None:
    response = await client.post(
        f"{url}/rest/v1/rpc/even_odd_claim_collection_job",
        headers=_headers(key),
        json={"p_worker_id": WORKER_ID},
    )
    response.raise_for_status()
    result = response.json()
    if isinstance(result, list):
        return result[0] if result else None
    return result if isinstance(result, dict) and result.get("id") else None


async def _patch_job(client: httpx.AsyncClient, url: str, key: str, job_id: str, values: dict) -> dict | None:
    response = await client.patch(
        f"{url}/rest/v1/even_odd_collection_jobs",
        params={"id": f"eq.{job_id}", "worker_id": f"eq.{WORKER_ID}"},
        headers=_headers(key, "return=representation"),
        json={**values, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    response.raise_for_status()
    rows = response.json()
    return rows[0] if isinstance(rows, list) and rows else None


async def _run_job(job: dict, client: httpx.AsyncClient, url: str, key: str) -> None:
    job_id = str(job["id"])
    symbol = str(job["market_symbol"])
    target = int(job["target_ticks"])
    received = int(job.get("received_ticks") or 0)
    batch: list[dict] = []
    try:
        async with websockets.connect(
            DERIV_PUBLIC_WS_URL, ping_interval=20, ping_timeout=20,
            open_timeout=15, close_timeout=5, max_size=1_000_000,
        ) as ws:
            await ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 8801}))
            while received < target:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    state = await client.get(
                        f"{url}/rest/v1/even_odd_collection_jobs",
                        params={"select": "status,received_ticks,target_ticks",
                                "id": f"eq.{job_id}", "worker_id": f"eq.{WORKER_ID}", "limit": "1"},
                        headers=_headers(key),
                    )
                    state.raise_for_status()
                    rows = state.json()
                    if not rows or rows[0]["status"] != "collecting":
                        break
                    await _patch_job(client, url, key, job_id, {})
                    continue

                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    continue
                if msg.get("error"):
                    detail = msg["error"].get("message", "Deriv subscription failed.")
                    raise RuntimeError(str(detail))
                if msg.get("msg_type") != "tick" or not isinstance(msg.get("tick"), dict):
                    continue
                tick = msg["tick"]
                if tick.get("symbol") != symbol:
                    continue
                sub = msg.get("subscription")
                sub_id = str(sub.get("id")) if isinstance(sub, dict) and sub.get("id") is not None else None
                batch.append(normalize_live_tick(tick, symbol, subscription_id=sub_id))
                received += 1

                if len(batch) >= BATCH_SIZE or received >= target:
                    await persist_live_tick_batch(symbol=symbol, ticks=batch, worker_id=WORKER_ID, job_id=job_id)
                    batch = []
                    await _patch_job(client, url, key, job_id, {"received_ticks": received})

                if received % 25 == 0:
                    state = await client.get(
                        f"{url}/rest/v1/even_odd_collection_jobs",
                        params={"select": "status", "id": f"eq.{job_id}",
                                "worker_id": f"eq.{WORKER_ID}", "limit": "1"},
                        headers=_headers(key),
                    )
                    state.raise_for_status()
                    rows = state.json()
                    if not rows or rows[0]["status"] != "collecting":
                        break
                    await _patch_job(client, url, key, job_id, {})

        if batch:
            await persist_live_tick_batch(symbol=symbol, ticks=batch, worker_id=WORKER_ID)
        state = await client.get(
            f"{url}/rest/v1/even_odd_collection_jobs",
            params={"select": "status", "id": f"eq.{job_id}", "worker_id": f"eq.{WORKER_ID}", "limit": "1"},
            headers=_headers(key),
        )
        state.raise_for_status()
        rows = state.json()
        current_status = rows[0]["status"] if rows else "failed"
        final_status = "completed" if received >= target else ("stopped" if current_status == "stopped" else "failed")
        await _patch_job(client, url, key, job_id, {
            "received_ticks": received, "status": final_status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        if batch:
            try:
                await persist_live_tick_batch(symbol=symbol, ticks=batch, worker_id=WORKER_ID)
            except Exception:
                pass
        await _patch_job(client, url, key, job_id, {
            "received_ticks": received, "status": "failed",
            "last_error": str(exc)[:500],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"Collection job {job_id} failed: {exc}")


async def run_worker() -> None:
    url, key = _configuration()
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
        while True:
            try:
                job = await _claim(client, url, key)
                if job:
                    await _run_job(job, client, url, key)
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Manual collection worker error: {exc}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run_worker())
