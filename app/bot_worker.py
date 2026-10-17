"""Persistent, Demo-only bot execution worker.

Run as a separate long-lived process:
    python -m app.bot_worker

Safety properties:
- Claims queued runs with compare-and-set updates.
- Writes a trade ledger row before proposal/buy operations.
- Never retries a buy request after it may have been sent.
- Treats a lost buy acknowledgement as OUTCOME_UNKNOWN and blocks the run.
- On restart, active runs are marked interrupted or reconciliation_required; no order is replayed.
- Uses the same server-side encrypted OAuth credential store as the FastAPI gateway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import HTTPException

from app.bot_control import DemoBotConfig
from app.deriv_gateway import _owned_credential

LOG = logging.getLogger("tradebridge.bot_worker")
POLL_SECONDS = float(os.getenv("BOT_WORKER_POLL_SECONDS", "2"))
HEARTBEAT_SECONDS = 10
DERIV_ACCOUNT_URL = "https://api.derivws.com/trading/v1/options/accounts"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerStorage:
    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.url or not self.key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")

    async def request(
        self, method: str, table: str, *, params: dict[str, str] | None = None,
        payload: Any = None, prefer: str | None = None,
    ) -> Any:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.request(
                method, f"{self.url}/rest/v1/{table}", params=params,
                headers=headers, json=payload,
            )
        if not response.is_success:
            # Do not log response bodies: they may contain sensitive provider data.
            raise RuntimeError(f"Supabase {method} {table} failed ({response.status_code}).")
        return response.json() if response.content else None

    async def patch_run(self, run_id: str, patch: dict[str, Any], expected_status: str | None = None) -> bool:
        params = {"id": f"eq.{run_id}", "select": "id,status"}
        if expected_status:
            params["status"] = f"eq.{expected_status}"
        rows = await self.request("PATCH", "bot_runs", params=params, payload={**patch, "updated_at": now_iso()}, prefer="return=representation")
        return isinstance(rows, list) and bool(rows)

    async def patch_trade(self, trade_id: str, patch: dict[str, Any]) -> None:
        rows = await self.request("PATCH", "bot_trades", params={"id": f"eq.{trade_id}", "select": "id"}, payload={**patch, "updated_at": now_iso()}, prefer="return=representation")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Could not persist bot trade state.")

    async def insert_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = await self.request("POST", "bot_trades", params={"select": "*"}, payload=payload, prefer="return=representation")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Could not persist bot trade before execution.")
        return rows[0]


async def provider_demo_socket(account_id: str, user_id: str):
    """Create a short-lived Demo trading socket URL without exposing OAuth tokens."""
    credential = await _owned_credential(user_id)
    async with httpx.AsyncClient(timeout=10) as client:
        accounts_response = await client.get(
            DERIV_ACCOUNT_URL,
            headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
        )
        if not accounts_response.is_success:
            raise RuntimeError("Deriv account verification failed.")
        payload = accounts_response.json()
        root = payload if isinstance(payload, dict) else {}
        data = root.get("data")
        accounts = data if isinstance(data, list) else data.get("accounts", []) if isinstance(data, dict) else root.get("accounts", [])
        account = next((a for a in accounts if isinstance(a, dict) and a.get("account_id") == account_id), None)
        if not account or str(account.get("account_type", "")).lower() != "demo":
            raise RuntimeError("Worker permits Demo accounts only.")
        otp_response = await client.post(
            f"{DERIV_ACCOUNT_URL}/{account_id}/otp",
            headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
        )
        if not otp_response.is_success:
            raise RuntimeError("Could not create a Demo trading session.")
        otp_payload = otp_response.json()
    root = otp_payload if isinstance(otp_payload, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else {}
    ws_url = data.get("url")
    if not isinstance(ws_url, str):
        raise RuntimeError("Deriv returned an invalid trading session.")
    parsed = urlparse(ws_url)
    if parsed.scheme != "wss" or parsed.hostname != "api.derivws.com" or parsed.path != "/trading/v1/options/ws/demo" or not parsed.query:
        raise RuntimeError("Deriv trading session was not a verified Demo WebSocket.")
    return ws_url


async def recv_response(ws, req_id: int, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for Deriv response.")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            continue
        if msg.get("req_id") != req_id:
            # Subscription updates may arrive while waiting for a correlated response.
            continue
        if msg.get("error"):
            err = msg["error"]
            code = err.get("code", "DerivError") if isinstance(err, dict) else "DerivError"
            raise RuntimeError(f"Deriv rejected the request ({code}).")
        return msg


async def send_request(ws, req: dict[str, Any], req_id: int) -> dict[str, Any]:
    await ws.send(json.dumps({**req, "req_id": req_id}))
    return await recv_response(ws, req_id)


async def mark_startup_recovery(storage: WorkerStorage) -> None:
    """Never replay a run left active by a prior process."""
    runs = await storage.request("GET", "bot_runs", params={
        "select": "id,status", "status": "in.(running,stopping)",
        "limit": "500",
    })
    if not isinstance(runs, list):
        return
    for run in runs:
        run_id = run.get("id")
        if not isinstance(run_id, str):
            continue
        trades = await storage.request("GET", "bot_trades", params={
            "select": "id,state,contract_id", "run_id": f"eq.{run_id}",
            "state": "in.(buy_pending,outcome_unknown,open,settling)",
        })
        unresolved = isinstance(trades, list) and bool(trades)
        target = "reconciliation_required" if unresolved else "interrupted"
        await storage.patch_run(run_id, {
            "status": target,
            "stop_reason": "Worker restarted; no trade was replayed. Reconcile Deriv state before restarting." if unresolved else "Worker restarted before the run completed.",
            "ended_at": now_iso() if not unresolved else None,
        }, expected_status=run.get("status"))
        if unresolved and isinstance(trades, list):
            for trade in trades:
                if trade.get("state") in {"buy_pending", "outcome_unknown"}:
                    await storage.patch_trade(trade["id"], {
                        "state": "outcome_unknown",
                        "error_code": "WORKER_RESTART_RECONCILIATION_REQUIRED",
                        "error_message": "Worker restarted after a possible purchase; automatic retry is prohibited.",
                    })


async def process_run(storage: WorkerStorage, run: dict[str, Any]) -> None:
    run_id = run["id"]
    user_id = run["user_id"]
    account_id = run["account_id"]
    config = DemoBotConfig.model_validate(run["config"])
    config.validate_contract()
    if config.stakeStrategy.type != "fixed":
        raise RuntimeError("Only fixed-stake strategies are permitted by the server worker.")
    if config.durationUnit != "t":
        raise RuntimeError("Only tick-duration contracts are permitted.")
    if not await storage.patch_run(run_id, {"status": "running", "started_at": now_iso(), "heartbeat_at": now_iso()}, expected_status="queued"):
        return

    started = time.monotonic()
    trade_count = int(run.get("trade_count") or 0)
    wins = int(run.get("wins") or 0)
    losses = int(run.get("losses") or 0)
    pnl = float(run.get("realized_pnl") or 0)
    consecutive_losses = 0
    try:
        ws_url = await provider_demo_socket(account_id, user_id)
        async with websockets.connect(ws_url, open_timeout=12, close_timeout=3, ping_interval=20, ping_timeout=20, max_size=1_000_000) as ws:
            while True:
                latest = await storage.request("GET", "bot_runs", params={"select": "status", "id": f"eq.{run_id}", "limit": "1"})
                status = latest[0].get("status") if isinstance(latest, list) and latest else "stopping"
                if status == "stopping":
                    await storage.patch_run(run_id, {"status": "stopped", "ended_at": now_iso(), "stop_reason": "Stop requested."}, expected_status="stopping")
                    return
                if status != "running":
                    return
                if trade_count >= config.maxTradesPerDay:
                    await storage.patch_run(run_id, {"status": "completed", "ended_at": now_iso(), "stop_reason": "Maximum trades reached."}, expected_status="running")
                    return
                if any(s.type == "time" and time.monotonic() - started >= s.seconds for s in config.stopConditions):
                    await storage.patch_run(run_id, {"status": "completed", "ended_at": now_iso(), "stop_reason": "Time limit reached."}, expected_status="running")
                    return
                if any(s.type == "loss" and pnl <= -s.limit for s in config.stopConditions):
                    await storage.patch_run(run_id, {"status": "completed", "ended_at": now_iso(), "stop_reason": "Loss limit reached."}, expected_status="running")
                    return
                if any(s.type == "profit" and pnl >= s.target for s in config.stopConditions):
                    await storage.patch_run(run_id, {"status": "completed", "ended_at": now_iso(), "stop_reason": "Profit target reached."}, expected_status="running")
                    return
                if consecutive_losses >= config.maxConsecutiveLosses:
                    await storage.patch_run(run_id, {"status": "completed", "ended_at": now_iso(), "stop_reason": "Consecutive loss limit reached."}, expected_status="running")
                    return

                trade_id = str(uuid.uuid4())
                idempotency_key = str(uuid.uuid4())
                trade = await storage.insert_trade({
                    "run_id": run_id, "user_id": user_id, "account_id": account_id,
                    "idempotency_key": idempotency_key, "state": "created",
                    "contract_type": config.contractType, "symbol": config.symbol,
                    "stake": config.stakeStrategy.amount, "currency": run.get("currency") or "USD",
                })
                await storage.patch_trade(trade["id"], {"state": "requesting_proposal", "proposal_requested_at": now_iso()})
                proposal_req = {
                    "proposal": 1, "amount": config.stakeStrategy.amount, "basis": "stake",
                    "contract_type": config.contractType, "currency": run.get("currency") or "USD",
                    "duration": config.duration, "duration_unit": config.durationUnit,
                    "underlying_symbol": config.symbol,
                    **({"barrier": config.barrier} if config.barrier is not None else {}),
                }
                try:
                    proposal_msg = await send_request(ws, proposal_req, int(time.time() * 1000) % 2_000_000_000)
                except Exception as exc:
                    await storage.patch_trade(trade["id"], {"state": "failed", "error_code": "PROPOSAL_FAILED", "error_message": str(exc)[:300]})
                    raise
                proposal = proposal_msg.get("proposal") if isinstance(proposal_msg.get("proposal"), dict) else {}
                proposal_id = proposal.get("id")
                if not isinstance(proposal_id, str) or not proposal_id:
                    await storage.patch_trade(trade["id"], {"state": "failed", "error_code": "INVALID_PROPOSAL", "error_message": "Deriv returned no proposal ID."})
                    raise RuntimeError("Deriv returned no proposal ID.")
                await storage.patch_trade(trade["id"], {"proposal_id": proposal_id, "state": "buy_pending", "buy_requested_at": now_iso()})
                buy_req_id = int(time.time() * 1000) % 2_000_000_000
                # Critical boundary: after send() starts, any timeout/disconnect is ambiguous.
                try:
                    await ws.send(json.dumps({"buy": proposal_id, "price": config.stakeStrategy.amount, "req_id": buy_req_id}))
                    buy_msg = await recv_response(ws, buy_req_id, timeout=12)
                except Exception as exc:
                    await storage.patch_trade(trade["id"], {
                        "state": "outcome_unknown", "error_code": "BUY_ACK_UNKNOWN",
                        "error_message": "Buy may have been accepted; automatic retry is prohibited.",
                    })
                    await storage.patch_run(run_id, {
                        "status": "reconciliation_required",
                        "stop_reason": "Purchase acknowledgement was uncertain. Check Deriv contract/account history before resuming.",
                    }, expected_status="running")
                    LOG.error("Run %s requires purchase reconciliation (%s)", run_id, type(exc).__name__)
                    return
                buy = buy_msg.get("buy") if isinstance(buy_msg.get("buy"), dict) else {}
                contract_id = buy.get("contract_id")
                if contract_id is None:
                    await storage.patch_trade(trade["id"], {"state": "outcome_unknown", "error_code": "BUY_ACK_MISSING_CONTRACT"})
                    await storage.patch_run(run_id, {"status": "reconciliation_required", "stop_reason": "Buy acknowledgement lacked a contract ID."}, expected_status="running")
                    return
                contract_id = str(contract_id)
                await storage.patch_trade(trade["id"], {"state": "open", "contract_id": contract_id, "opened_at": now_iso(), "broker_status": "open"})
                open_req_id = (buy_req_id + 1) % 2_000_000_000
                try:
                    await ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": int(contract_id), "subscribe": 1, "req_id": open_req_id}))
                    # The initial response and subsequent updates share req_id; wait for settlement only.
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        msg = json.loads(raw)
                        if not isinstance(msg, dict):
                            continue
                        if msg.get("error"):
                            raise RuntimeError("Deriv contract monitor returned an error.")
                        poc = msg.get("proposal_open_contract")
                        if not isinstance(poc, dict) or str(poc.get("contract_id")) != contract_id:
                            continue
                        await storage.patch_run(run_id, {"heartbeat_at": now_iso()})
                        if poc.get("is_sold") or poc.get("is_expired"):
                            profit = float(poc.get("profit", 0))
                            payout = float(poc.get("payout", 0))
                            await storage.patch_trade(trade["id"], {
                                "state": "settled", "broker_status": str(poc.get("status", "settled")),
                                "profit": profit, "payout": payout, "settled_at": now_iso(),
                            })
                            break
                except Exception as exc:
                    await storage.patch_trade(trade["id"], {
                        "state": "settling", "error_code": "SETTLEMENT_MONITOR_INTERRUPTED",
                        "error_message": "Contract exists but settlement is not confirmed; reconcile before another trade.",
                    })
                    await storage.patch_run(run_id, {
                        "status": "reconciliation_required",
                        "stop_reason": "Contract settlement could not be confirmed; no further trades will be submitted.",
                    }, expected_status="running")
                    LOG.error("Run %s contract %s needs settlement reconciliation (%s)", run_id, contract_id, type(exc).__name__)
                    return

                trade_count += 1
                pnl += profit
                if profit > 0:
                    wins += 1
                    consecutive_losses = 0
                else:
                    losses += 1
                    consecutive_losses += 1
                await storage.patch_run(run_id, {
                    "trade_count": trade_count, "wins": wins, "losses": losses,
                    "realized_pnl": pnl, "heartbeat_at": now_iso(),
                }, expected_status="running")
                await asyncio.sleep(config.cooldownSeconds)
    except HTTPException as exc:
        await storage.patch_run(run_id, {"status": "failed", "ended_at": now_iso(), "stop_reason": f"Authorization/configuration failure ({exc.status_code})."}, expected_status="running")
    except Exception as exc:
        # If a trade was in buy/open/settling, startup recovery will preserve the lock.
        LOG.exception("Run %s failed in worker (%s)", run_id, type(exc).__name__)
        latest = await storage.request("GET", "bot_trades", params={
            "select": "state", "run_id": f"eq.{run_id}", "order": "created_at.desc", "limit": "1",
        })
        unsafe = isinstance(latest, list) and bool(latest) and latest[0].get("state") in {"buy_pending", "outcome_unknown", "open", "settling"}
        await storage.patch_run(run_id, {
            "status": "reconciliation_required" if unsafe else "failed",
            "ended_at": None if unsafe else now_iso(),
            "stop_reason": "Execution interrupted; reconcile Deriv before resuming." if unsafe else "Worker could not execute the run; no trade was retried.",
        }, expected_status="running")


async def worker_loop() -> None:
    storage = WorkerStorage()
    await mark_startup_recovery(storage)
    LOG.info("TradeBridge Demo bot worker started; real-account execution is not supported.")
    while True:
        try:
            rows = await storage.request("GET", "bot_runs", params={
                "select": "*", "status": "eq.queued", "order": "created_at.asc", "limit": "1",
            })
            if isinstance(rows, list) and rows:
                run = rows[0]
                # process_run performs a compare-and-set claim before any provider operation.
                await process_run(storage, run)
            else:
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error("Worker loop error (%s); retrying queue poll after delay.", type(exc).__name__)
            await asyncio.sleep(max(POLL_SECONDS, 5))


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
