"""Persistent, Demo-only execution worker for durable bot runs.

Run as a separate long-lived process: python -m app.bot_worker.
A worker never retries a buy whose acknowledgement is ambiguous. It persists BUY_PENDING
before sending the buy request and moves ambiguous outcomes to OUTCOME_UNKNOWN, which
requires broker reconciliation before another trade is permitted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import HTTPException

from app.deriv_gateway import _linked_account_ids, _load_credential

LOG = logging.getLogger("tradebridge.bot_worker")
TERMINAL_RUNS = {"stopped", "completed", "failed", "interrupted"}
ACTIVE_TRADE_STATES = {"buy_pending", "outcome_unknown", "open", "settling"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerError(RuntimeError):
    pass


class DurableBotWorker:
    def __init__(self) -> None:
        self.supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.poll_seconds = float(os.getenv("BOT_WORKER_POLL_SECONDS", "2"))
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:10]}"
        if not self.supabase_url or not self.service_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required.")
        # Keep the reusable HTTP client free of database credentials: it also calls Deriv.
        # Supabase service-role headers are attached only inside db(), never to upstream requests.
        self.http = httpx.AsyncClient(timeout=10.0)
        self.stopping = asyncio.Event()
        self.last_heartbeat = 0.0

    async def close(self) -> None:
        self.stopping.set()
        await self.http.aclose()

    async def db(self, method: str, table: str, *, params: dict[str, str] | None = None,
                 body: Any = None, prefer: str | None = None) -> Any:
        headers = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        response = await self.http.request(method, f"{self.supabase_url}/rest/v1/{table}",
                                            params=params, json=body, headers=headers)
        if not response.is_success:
            raise WorkerError(f"Database {method} {table} failed with HTTP {response.status_code}.")
        return response.json() if response.content else None

    async def rows(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        result = await self.db("GET", table, params=params)
        if not isinstance(result, list):
            raise WorkerError(f"Unexpected response reading {table}.")
        return result

    async def patch_one(self, table: str, params: dict[str, str], body: dict[str, Any]) -> dict[str, Any] | None:
        result = await self.db("PATCH", table, params={**params, "select": "*"}, body=body,
                               prefer="return=representation")
        return result[0] if isinstance(result, list) and result else None

    async def claim_next(self) -> dict[str, Any] | None:
        queued = await self.rows("bot_runs", {
            "select": "*", "status": "eq.queued", "order": "created_at.asc", "limit": "10",
        })
        for run in queued:
            claimed = await self.patch_one("bot_runs", {
                "id": f"eq.{run['id']}", "status": "eq.queued",
            }, {"status": "running", "started_at": run.get("started_at") or utc_now(), "heartbeat_at": utc_now(),
                "stop_reason": None, "version": int(run.get("version") or 0) + 1})
            if claimed:
                LOG.info("claimed run_id=%s worker_id=%s", claimed["id"], self.worker_id)
                return claimed
        return None

    async def set_run(self, run_id: str, status: str, **fields: Any) -> None:
        updated = await self.patch_one("bot_runs", {"id": f"eq.{run_id}"}, {
            "status": status, "updated_at": utc_now(), "heartbeat_at": utc_now(), **fields,
        })
        if updated is None:
            raise WorkerError(f"Could not persist run state for {run_id}.")

    async def demo_credential(self, run: dict[str, Any]):
        credential = await _load_credential(str(run["user_id"]))
        if credential is None or credential.expires_at <= time.time() + 15:
            raise WorkerError("Deriv credential is missing or expired; run stopped without trading.")
        response = await self.http.get(
            "https://api.derivws.com/trading/v1/options/accounts",
            headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
        )
        if not response.is_success:
            raise WorkerError("Could not re-verify the Deriv account; execution blocked.")
        payload = response.json()
        root = payload if isinstance(payload, dict) else {}
        data = root.get("data")
        accounts = data if isinstance(data, list) else data.get("accounts", []) if isinstance(data, dict) else root.get("accounts", [])
        account = next((a for a in accounts if isinstance(a, dict) and a.get("account_id") == run["account_id"]), None)
        if not account or str(account.get("account_type", "")).lower() != "demo":
            raise WorkerError("Worker permits Demo accounts only.")
        try:
            linked = await _linked_account_ids(str(run["user_id"]), [str(run["account_id"])])
        except HTTPException as exc:
            raise WorkerError("Could not verify account ownership; execution blocked.") from exc
        if str(run["account_id"]) not in linked:
            raise WorkerError("Deriv account is no longer linked to this user; execution blocked.")
        max_stake = float(os.getenv("TRADEBRIDGE_DEMO_MAX_STAKE", "1000"))
        stake = float(run.get("config", {}).get("stakeStrategy", {}).get("amount", 0))
        if not 0 < stake <= max_stake:
            raise WorkerError("Configured stake exceeds the server-side Demo stake limit.")
        return credential

    async def open_socket(self, run: dict[str, Any], credential: Any):
        """Open only the authenticated Demo Options socket issued for this linked account."""
        response = await self.http.post(
            f"https://api.derivws.com/trading/v1/options/accounts/{run['account_id']}/otp",
            headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
        )
        if not response.is_success:
            raise WorkerError("Deriv did not issue a Demo session URL; execution blocked.")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        provider_url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(provider_url, str):
            raise WorkerError("Deriv returned an invalid Demo session URL.")
        parsed = urlparse(provider_url)
        if parsed.scheme != "wss" or parsed.hostname != "api.derivws.com" or parsed.path != "/trading/v1/options/ws/demo":
            raise WorkerError("Deriv session URL did not match the approved Demo endpoint.")
        return await websockets.connect(
            provider_url, open_timeout=10, close_timeout=3, ping_interval=20,
            ping_timeout=20, max_size=2**20,
        )

    async def request(self, ws, payload: dict[str, Any], timeout: float = 12) -> dict[str, Any]:
        req_id = int(time.time_ns() % 2_000_000_000)
        request = {**payload, "req_id": req_id}
        await ws.send(json.dumps(request))
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for Deriv {next(iter(payload))} response.")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            if msg.get("req_id") != req_id:
                continue
            if msg.get("error"):
                error = msg["error"]
                raise WorkerError(f"Deriv rejected {next(iter(payload))}: {error.get('message', 'unknown error')}")
            return msg

    def worker_heartbeat_healthy(self) -> bool:
        """Fail closed if the durable worker heartbeat is missing or stale."""
        return self.last_heartbeat > 0 and time.time() - self.last_heartbeat <= 30

    async def may_submit_buy(self, run_id: str) -> bool:
        """Recheck liveness and durable run status immediately before any purchase."""
        if not self.worker_heartbeat_healthy():
            raise WorkerError("Worker heartbeat is missing or stale; new purchases are paused.")
        current = await self.rows("bot_runs", {
            "select": "status,stop_reason", "id": f"eq.{run_id}", "limit": "1",
        })
        if not current:
            raise WorkerError("Run disappeared before purchase; execution blocked.")
        status = current[0].get("status")
        if status == "stopping":
            await self.set_run(run_id, "stopped", ended_at=utc_now(),
                               stop_reason=current[0].get("stop_reason") or "User requested stop before purchase.")
            return False
        if status != "running":
            raise WorkerError(f"Run is no longer running ({status}); purchase blocked.")
        return True

    async def create_trade(self, run: dict[str, Any]) -> dict[str, Any]:
        config = run["config"]
        key = uuid.uuid4().hex
        payload = {
            "run_id": run["id"], "user_id": run["user_id"], "account_id": run["account_id"],
            "idempotency_key": key, "state": "created", "contract_type": config["contractType"],
            "symbol": config["symbol"], "stake": config["stakeStrategy"]["amount"],
            "currency": run.get("currency") or "USD",
        }
        rows = await self.db("POST", "bot_trades", params={"select": "*"}, body=payload,
                             prefer="return=representation")
        if not isinstance(rows, list) or not rows:
            raise WorkerError("Could not persist trade ledger before execution.")
        return rows[0]

    async def trade_patch(self, trade_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = await self.patch_one("bot_trades", {"id": f"eq.{trade_id}"}, {**body, "updated_at": utc_now()})
        if row is None:
            raise WorkerError("Could not persist trade ledger update.")
        return row

    async def sync_run_totals(self, run_id: str) -> dict[str, Any]:
        """Rebuild run counters from settled ledger rows; safe to repeat after a crash."""
        settled = await self.rows("bot_trades", {
            "select": "profit,state", "run_id": f"eq.{run_id}",
            "state": "eq.settled", "limit": "1000",
        })
        profits = [float(row.get("profit") or 0) for row in settled]
        updated = await self.patch_one("bot_runs", {"id": f"eq.{run_id}"}, {
            "trade_count": len(profits),
            "wins": sum(1 for profit in profits if profit > 0),
            "losses": sum(1 for profit in profits if profit <= 0),
            "realized_pnl": sum(profits),
            "heartbeat_at": utc_now(),
            "updated_at": utc_now(),
        })
        if updated is None:
            raise WorkerError("Could not reconcile run counters from the settled trade ledger.")
        return updated

    async def risk_stop_reason(self, run: dict[str, Any]) -> str | None:
        config = run.get("config") or {}
        count = int(run.get("trade_count") or 0)
        pnl = float(run.get("realized_pnl") or 0)
        if count >= int(config.get("maxTradesPerDay") or 20):
            return "Maximum trade count reached."
        for condition in config.get("stopConditions", []):
            kind = condition.get("type")
            if kind == "tradeCount" and count >= int(condition.get("max") or 1):
                return "Configured trade-count limit reached."
            if kind == "loss" and pnl <= -float(condition.get("limit") or 0):
                return "Configured loss limit reached."
            if kind == "profit" and pnl >= float(condition.get("target") or 0):
                return "Configured profit target reached."
            if kind == "time":
                try:
                    started_at = datetime.fromisoformat(str(run.get("started_at")).replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError):
                    started_at = time.time()
                if time.time() - started_at >= float(condition.get("seconds") or 0):
                    return "Configured time limit reached."
        max_consecutive = int(config.get("maxConsecutiveLosses") or 3)
        recent = await self.rows("bot_trades", {
            "select": "profit,state", "run_id": f"eq.{run['id']}",
            "state": "eq.settled", "order": "settled_at.desc", "limit": str(max_consecutive),
        })
        consecutive_losses = 0
        for settled in recent:
            if float(settled.get("profit") or 0) <= 0:
                consecutive_losses += 1
            else:
                break
        if consecutive_losses >= max_consecutive:
            return "Maximum consecutive-loss limit reached."
        return None

    async def monitor_contract(self, ws, run: dict[str, Any], trade: dict[str, Any]) -> bool:
        contract_id = trade.get("contract_id")
        if not contract_id:
            return False
        monitor_attempts = int(trade.get("monitor_attempts") or 0) + 1
        await self.trade_patch(trade["id"], {"state": "settling", "monitor_attempts": monitor_attempts})
        try:
            await ws.send(json.dumps({"proposal_open_contract": 1, "contract_id": int(contract_id),
                                      "subscribe": 1, "req_id": int(time.time_ns() % 2_000_000_000)}))
            while not self.stopping.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=35)
                msg = json.loads(raw)
                contract = msg.get("proposal_open_contract")
                if not isinstance(contract, dict) or str(contract.get("contract_id")) != str(contract_id):
                    continue
                await self.patch_one("bot_runs", {"id": f"eq.{run['id']}"}, {"heartbeat_at": utc_now(), "updated_at": utc_now()})
                if contract.get("is_sold") or contract.get("is_expired") or contract.get("status") in {"won", "lost", "sold", "expired", "cancelled"}:
                    payout = float(contract.get("payout") or 0)
                    profit = float(contract.get("profit") or 0)
                    await self.trade_patch(trade["id"], {
                        "state": "settled", "payout": payout, "profit": profit,
                        "broker_status": str(contract.get("status") or "sold"), "settled_at": utc_now(),
                    })
                    latest = await self.sync_run_totals(str(run["id"]))
                    stop_reason = await self.risk_stop_reason(latest)
                    if latest.get("status") == "stopping" or stop_reason:
                        await self.set_run(run["id"], "stopped", ended_at=utc_now(),
                                           stop_reason=latest.get("stop_reason") if latest.get("status") == "stopping" else stop_reason)
                        return False
                    return True
        except (TimeoutError, websockets.ConnectionClosed, OSError):
            await self.trade_patch(trade["id"], {"state": "outcome_unknown", "monitor_attempts": monitor_attempts,
                                                 "error_message": "Lost connection while monitoring; reattach to this known contract before placing another trade."})
            current = await self.rows("bot_runs", {"select": "status", "id": f"eq.{run['id']}", "limit": "1"})
            current_status = current[0].get("status") if current else None
            if current_status == "stopping":
                await self.set_run(run["id"], "reconciliation_required",
                                   stop_reason="Stop was requested while the contract connection was lost; reconcile the known contract before proceeding.")
            elif monitor_attempts < 4:
                await self.set_run(run["id"], "queued",
                                   stop_reason="Connection interrupted while monitoring a known contract; worker will resume monitoring without buying.")
            else:
                await self.set_run(run["id"], "reconciliation_required",
                                   stop_reason="Repeated failures monitoring a known contract; manual broker reconciliation required.")
            return False
        return False

    async def execute_run(self, run: dict[str, Any]) -> None:
        run_id = str(run["id"])
        try:
            config = run["config"]
            # The worker intentionally rejects stake progression systems; only fixed stake is supported.
            if config.get("stakeStrategy", {}).get("type") != "fixed":
                raise WorkerError("Only fixed stake is supported by the server worker.")
            if config.get("durationUnit") != "t":
                raise WorkerError("Only tick-duration digit contracts are supported.")
            if config.get("contractType") not in {"DIGITOVER", "DIGITUNDER", "DIGITEVEN", "DIGITODD", "DIGITMATCH", "DIGITDIFF"}:
                raise WorkerError("Unsupported contract type.")
            credential = await self.demo_credential(run)
            # Recovery gate: any existing unresolved trade blocks new buys.
            prior = await self.rows("bot_trades", {"select": "*", "run_id": f"eq.{run_id}",
                                                   "order": "created_at.desc", "limit": "1"})
            if prior:
                trade = prior[0]
                if trade.get("state") in {"buy_pending", "outcome_unknown"} and not trade.get("contract_id"):
                    await self.trade_patch(trade["id"], {"state": "outcome_unknown",
                                                         "error_message": "Restart recovery cannot prove whether Deriv accepted the buy."})
                    await self.set_run(run_id, "reconciliation_required",
                                       stop_reason="Ambiguous buy after worker restart; manual broker reconciliation required.")
                    return
                if trade.get("state") in {"open", "settling", "outcome_unknown"} and trade.get("contract_id"):
                    ws = await self.open_socket(run, credential)
                    try:
                        should_continue = await self.monitor_contract(ws, run, trade)
                    finally:
                        await ws.close()
                    if not should_continue:
                        return
                    # The known contract is conclusively settled; continue through the
                    # normal risk-gated loop using a fresh authenticated Demo session.
                    trade["state"] = "settled"
                if trade.get("state") not in {"settled", "failed"}:
                    await self.set_run(run_id, "reconciliation_required",
                                       stop_reason="Existing trade state requires reconciliation.")
                    return
            ws = await self.open_socket(run, credential)
            try:
                while not self.stopping.is_set():
                    # Existing contracts may still be monitored after heartbeat loss,
                    # but a new purchase must never be initiated by a stale worker.
                    current = await self.rows("bot_runs", {"select": "*", "id": f"eq.{run_id}", "limit": "1"})
                    if not current:
                        return
                    run = current[0]
                    if run.get("status") == "stopping":
                        await self.set_run(run_id, "stopped", ended_at=utc_now(), stop_reason=run.get("stop_reason") or "User requested stop.")
                        return
                    trade = await self.create_trade(run)
                    config = run["config"]
                    await self.trade_patch(trade["id"], {"state": "requesting_proposal", "proposal_requested_at": utc_now()})
                    proposal_payload = {
                        "proposal": 1, "amount": float(config["stakeStrategy"]["amount"]), "basis": "stake",
                        "contract_type": config["contractType"], "currency": run.get("currency") or "USD",
                        "duration": int(config["duration"]), "duration_unit": "t",
                        "underlying_symbol": config["symbol"],
                    }
                    if config.get("barrier") is not None:
                        proposal_payload["barrier"] = str(config["barrier"])
                    try:
                        proposal = await self.request(ws, proposal_payload, timeout=15)
                    except Exception as exc:
                        # Proposal requests have no purchase side effect; a timeout is safe to fail.
                        await self.trade_patch(trade["id"], {"state": "failed", "error_message": f"Proposal failed before buy ({type(exc).__name__})."})
                        raise
                    proposal_obj = proposal.get("proposal") or {}
                    proposal_id = proposal_obj.get("id")
                    ask_price = float(proposal_obj.get("ask_price") or 0)
                    if not proposal_id or ask_price <= 0:
                        await self.trade_patch(trade["id"], {"state": "failed", "error_message": "Invalid proposal response."})
                        raise WorkerError("Deriv returned an invalid proposal.")
                    # A stop request or stale worker heartbeat can arrive while the
                    # proposal is in flight. Recheck before the side-effecting buy.
                    if not await self.may_submit_buy(run_id):
                        await self.trade_patch(trade["id"], {
                            "state": "failed",
                            "error_message": "Purchase prevented because the run was stopped before buy.",
                        })
                        return
                    await self.trade_patch(trade["id"], {"state": "buy_pending", "proposal_id": str(proposal_id),
                                                         "buy_requested_at": utc_now()})
                    try:
                        bought = await self.request(ws, {"buy": str(proposal_id), "price": ask_price}, timeout=15)
                    except WorkerError:
                        # Explicit broker rejection is definitive; no contract was bought.
                        await self.trade_patch(trade["id"], {"state": "failed", "error_message": "Deriv explicitly rejected the buy."})
                        raise
                    except Exception as exc:
                        # A timeout/socket error after sending buy is ambiguous. Never retry it.
                        await self.trade_patch(trade["id"], {"state": "outcome_unknown",
                                                             "error_message": f"Buy acknowledgement uncertain ({type(exc).__name__}); reconcile with Deriv."})
                        await self.set_run(run_id, "reconciliation_required",
                                           stop_reason="Buy acknowledgement uncertain; duplicate purchase blocked.")
                        return
                    buy_obj = bought.get("buy") or {}
                    contract_id = buy_obj.get("contract_id")
                    if not contract_id:
                        await self.trade_patch(trade["id"], {"state": "outcome_unknown",
                                                             "error_message": "Buy response lacked a contract ID."})
                        await self.set_run(run_id, "reconciliation_required",
                                           stop_reason="Buy response cannot be reconciled safely.")
                        return
                    await self.trade_patch(trade["id"], {"state": "open", "contract_id": str(contract_id),
                                                         "opened_at": utc_now(), "broker_status": "open"})
                    again = await self.monitor_contract(ws, run, {**trade, "contract_id": str(contract_id)})
                    if not again:
                        return
                    await asyncio.sleep(max(1, min(int(config.get("cooldownSeconds") or 2), 60)))
            finally:
                await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.exception("run failed run_id=%s", run_id)
            try:
                # A generic exception after a side-effect may have started must not
                # mark the run safely failed and unlock another purchase.
                latest = await self.rows("bot_trades", {
                    "select": "*", "run_id": f"eq.{run_id}",
                    "order": "created_at.desc", "limit": "1",
                })
                if latest and latest[0].get("state") in ACTIVE_TRADE_STATES:
                    trade = latest[0]
                    if trade.get("state") == "buy_pending" and not trade.get("contract_id"):
                        await self.trade_patch(trade["id"], {
                            "state": "outcome_unknown",
                            "error_message": f"Worker exception after buy intent ({type(exc).__name__}); reconcile with Deriv.",
                        })
                        await self.set_run(run_id, "reconciliation_required", ended_at=utc_now(),
                                           stop_reason="Purchase outcome uncertain; new trades are locked pending reconciliation.")
                    elif trade.get("contract_id"):
                        await self.set_run(run_id, "queued",
                                           stop_reason="Worker interrupted while a known contract existed; resuming monitoring only.")
                    else:
                        await self.set_run(run_id, "reconciliation_required", ended_at=utc_now(),
                                           stop_reason="Worker interrupted during an unresolved trade; manual reconciliation required.")
                else:
                    await self.set_run(run_id, "failed", ended_at=utc_now(),
                                       stop_reason=f"Worker stopped safely: {type(exc).__name__}.")
            except Exception:
                LOG.exception("could not persist failure/recovery state run_id=%s", run_id)

    async def recover_stale_runs(self) -> None:
        # Never auto-resume a run whose worker heartbeat is stale: a crash can occur
        # after a buy request leaves the process but before the response is persisted.
        cutoff = time.time() - 45
        rows = await self.rows("bot_runs", {"select": "*", "status": "in.(running,stopping)", "limit": "100"})
        for run in rows:
            heartbeat = run.get("heartbeat_at")
            try:
                beat = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00")).timestamp() if heartbeat else 0
            except ValueError:
                beat = 0
            if beat >= cutoff:
                continue
            trades = await self.rows("bot_trades", {"select": "*", "run_id": f"eq.{run['id']}",
                                                    "order": "created_at.desc", "limit": "1"})
            if run.get("status") == "stopping":
                if trades and trades[0].get("state") == "settled":
                    latest = await self.sync_run_totals(str(run["id"]))
                    await self.set_run(run["id"], "stopped", ended_at=utc_now(),
                                       stop_reason=latest.get("stop_reason") or "Stop request completed after settlement recovery.")
                elif trades and trades[0].get("state") in ACTIVE_TRADE_STATES:
                    trade = trades[0]
                    if trade.get("state") == "buy_pending" and not trade.get("contract_id"):
                        await self.trade_patch(trade["id"], {
                            "state": "outcome_unknown",
                            "error_message": "Worker restarted after stop request while buy outcome was unknown.",
                        })
                    await self.set_run(run["id"], "reconciliation_required",
                                       stop_reason="Worker restarted after stop request with an unresolved trade; reconcile with Deriv before proceeding.")
                else:
                    await self.set_run(run["id"], "stopped", ended_at=utc_now(),
                                       stop_reason="Stop request recovered after worker restart.")
                continue
            if trades:
                trade = trades[0]
                if trade.get("state") in {"open", "settling", "outcome_unknown"} and trade.get("contract_id"):
                    # Known contract ID is sufficient to resume monitoring, never to place a replacement buy.
                    await self.set_run(run["id"], "queued",
                                       stop_reason="Worker restarted; resuming monitoring of the known contract.")
                elif trade.get("state") == "settled":
                    # A crash may happen after recording settlement but before updating run totals.
                    latest = await self.sync_run_totals(str(run["id"]))
                    stop_reason = await self.risk_stop_reason(latest)
                    if latest.get("status") == "stopping" or stop_reason:
                        await self.set_run(run["id"], "stopped", ended_at=utc_now(),
                                           stop_reason=latest.get("stop_reason") if latest.get("status") == "stopping" else f"Recovered settlement: {stop_reason}")
                    else:
                        await self.set_run(run["id"], "queued",
                                           stop_reason="Recovered settled ledger and reconciled run counters; continuing safely.")
                elif trade.get("state") in ACTIVE_TRADE_STATES:
                    if trade.get("state") == "buy_pending" and not trade.get("contract_id"):
                        await self.trade_patch(trade["id"], {
                            "state": "outcome_unknown",
                            "error_message": "Worker restarted after buy was persisted; Deriv acceptance is unknown.",
                        })
                    await self.set_run(run["id"], "reconciliation_required",
                                       stop_reason="Stale worker heartbeat with ambiguous purchase; manual reconciliation required.")
                else:
                    await self.set_run(run["id"], "interrupted", ended_at=utc_now(),
                                       stop_reason="Worker heartbeat expired before a trade was in flight.")
            else:
                await self.set_run(run["id"], "interrupted", ended_at=utc_now(),
                                   stop_reason="Worker heartbeat expired before a trade was in flight.")

    async def write_heartbeat(self) -> None:
        await self.db("POST", "bot_worker_heartbeats", params={"on_conflict": "worker_id"},
                      body={"worker_id": self.worker_id, "last_seen_at": utc_now()},
                      prefer="resolution=merge-duplicates,return=minimal")
        self.last_heartbeat = time.time()

    async def heartbeat_forever(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.write_heartbeat()
            except Exception:
                self.last_heartbeat = 0.0
                LOG.exception("worker heartbeat failed; execution paused")
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass

    async def run_forever(self) -> None:
        LOG.info("worker started worker_id=%s", self.worker_id)
        # Do not start consuming jobs until durable liveness is confirmed.
        await self.write_heartbeat()
        heartbeat_task = asyncio.create_task(self.heartbeat_forever(), name="bot-worker-heartbeat")
        try:
            while not self.stopping.is_set():
                try:
                    if time.time() - self.last_heartbeat > 30:
                        await asyncio.sleep(min(5, max(1, self.poll_seconds)))
                        continue
                    await self.recover_stale_runs()
                    run = await self.claim_next()
                    if run:
                        await self.execute_run(run)
                    else:
                        await asyncio.wait_for(self.stopping.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    LOG.exception("worker poll failed; fail-closed retry after delay")
                    await asyncio.sleep(min(10, max(2, self.poll_seconds)))
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = DurableBotWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.close()


if __name__ == "__main__":
    asyncio.run(main())
