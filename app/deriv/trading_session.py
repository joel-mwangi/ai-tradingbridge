"""Per-WebSocket trading-proxy session state.

Strategy/proposal state that previously lived as nonlocal closures inside
``deriv_websocket_proxy`` is held here as instance attributes so it can be
shared between the client-message and provider-message handlers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import time
from typing import Any

from fastapi import WebSocket

from app.market_analysis import DigitFrequencyWindow
from app.strategy.execution import StrategyExecution
from app.strategy.live_feed import LiveStrategyFeed
import app.deriv.credentials as _creds

_log = logging.getLogger(__name__)


class TradingProxySession:
    """Holds the per-connection strategy/contract state for the trading proxy."""

    def __init__(self, *, allowed_client_requests: set[str], account_type: str = "demo") -> None:
        self.allowed_client_requests = allowed_client_requests
        self.account_type = account_type.lower()
        self.real_trading_confirmed = False
        # State that persists across reconnects.
        self.pending_proposal_requests: set[int] = set()
        self.pending_strategy_proposals: set[int] = set()
        self.pending_strategy_buys: set[int] = set()
        self.strategy_request_deadlines: dict[int, tuple[str, float]] = {}
        self.valid_proposals: dict[str, float] = {}
        self.strategy_feed = LiveStrategyFeed()
        self.strategy_execution: StrategyExecution | None = None
        self.strategy_frequency = DigitFrequencyWindow()
        self.strategy_pip_size: int | None = None
        self.strategy_subscribed_symbol: str | None = None
        self.strategy_history_requests: dict[int, str] = {}
        self.active_poc_sub_id: str | None = None
        self.strategy_window_size = 1000
        # Settlement reconciliation: maps profit_table req_id → contract_id being checked.
        self.pending_settlement_checks: dict[int, str] = {}
        # Deadline (monotonic) after which we fall back to profit_table if not settled.
        self.settlement_fallback_deadline: float | None = None
        self.settlement_fallback_seconds: float = 3.0

    def next_strategy_request_id(self) -> int:
        request_id = secrets.randbelow(1_000_000) + 9_000_000
        while (
            request_id in self.pending_strategy_proposals
            or request_id in self.pending_strategy_buys
            or request_id in self.strategy_request_deadlines
            or request_id in self.pending_settlement_checks
        ):
            request_id = secrets.randbelow(1_000_000) + 9_000_000
        return request_id

    def register_strategy_deadline(self, request_id: int, phase: str) -> None:
        timeout = float(os.getenv("TRADEBRIDGE_STRATEGY_RESPONSE_TIMEOUT_SECONDS", "15"))
        if not math.isfinite(timeout) or timeout <= 0:
            timeout = 15.0
        self.strategy_request_deadlines[request_id] = (phase, time.monotonic() + timeout)

    async def resubscribe_ticks(self, upstream: Any) -> None:
        if self.strategy_subscribed_symbol:
            history_request_id = self.next_strategy_request_id()
            self.strategy_history_requests[history_request_id] = self.strategy_subscribed_symbol
            try:
                await upstream.send(json.dumps({
                    "ticks_history": self.strategy_subscribed_symbol,
                    "count": self.strategy_window_size,
                    "end": "latest",
                    "style": "ticks",
                    "req_id": history_request_id,
                }, separators=(",", ":")))
                await upstream.send(json.dumps(
                    {"ticks": self.strategy_subscribed_symbol, "subscribe": 1},
                    separators=(",", ":"),
                ))
            except Exception:
                pass

    def reset(self) -> None:
        self.pending_proposal_requests.clear()
        self.pending_strategy_proposals.clear()
        self.pending_strategy_buys.clear()
        self.strategy_request_deadlines.clear()
        self.valid_proposals.clear()
        self.strategy_feed = LiveStrategyFeed()
        self.strategy_execution = None
        self.strategy_frequency = DigitFrequencyWindow()
        self.strategy_pip_size = None
        self.strategy_subscribed_symbol = None
        self.strategy_history_requests.clear()
        self.active_poc_sub_id = None
        self.strategy_window_size = 1000
        self.pending_settlement_checks.clear()
        self.settlement_fallback_deadline = None

    async def strategy_response_timeout_monitor(self, websocket: WebSocket, upstream: Any) -> None:
        while True:
            await asyncio.sleep(0.25)
            if self.strategy_execution is None:
                continue
            now = time.monotonic()

            # Settlement fallback: if waiting for settlement too long, query profit_table.
            if (
                self.settlement_fallback_deadline is not None
                and now >= self.settlement_fallback_deadline
                and self.strategy_execution.state.value == "waiting_settlement"
                and self.strategy_execution.active_contract_id is not None
            ):
                self.settlement_fallback_deadline = None
                contract_id = self.strategy_execution.active_contract_id
                req_id = self.next_strategy_request_id()
                self.pending_settlement_checks[req_id] = contract_id
                _log.info("Settlement fallback: querying profit_table for contract %s", contract_id)
                try:
                    await upstream.send(json.dumps({
                        "profit_table": 1,
                        "description": 1,
                        "limit": 10,
                        "req_id": req_id,
                    }, separators=(",", ":")))
                except Exception:
                    self.pending_settlement_checks.pop(req_id, None)

            # Proposal/buy timeouts.
            expired = [
                rid for rid, (_, deadline) in self.strategy_request_deadlines.items()
                if deadline <= now
            ]
            for rid in expired:
                phase, _ = self.strategy_request_deadlines.pop(rid)
                self.pending_strategy_proposals.discard(rid)
                self.pending_strategy_buys.discard(rid)
                self.strategy_execution.block(f"Timed out waiting for Deriv {phase} response.")
                await websocket.send_json(
                    self.strategy_execution.status(
                        f"Automatic execution blocked: Deriv {phase} response timed out."
                    )
                )
                await websocket.send_json(self.strategy_feed.unlock_execution())
                self.strategy_execution.reset_for_next_signal()

    async def credential_expiry_monitor(self, websocket: WebSocket, user_id: str) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                cred = await _creds._load_credential(user_id)
            except Exception:
                continue
            if cred is None:
                continue
            if cred.expires_at - time.time() <= 300:
                try:
                    await websocket.send_json({
                        "type": "session_warning",
                        "message": "Deriv session expires in 5 minutes. Reconnect to continue trading.",
                    })
                except Exception:
                    return
