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
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

from app.market_analysis import DigitFrequencyWindow
from app.strategy.execution import StrategyExecution
from app.strategy.live_feed import LiveStrategyFeed
from app.strategy.digit_strategy import ContractType, evaluate_even_odd_setup
import app.deriv.credentials as _creds

_log = logging.getLogger(__name__)


EVEN_DIGITS: frozenset[int] = frozenset({0, 2, 4, 6, 8})
ODD_DIGITS: frozenset[int] = frozenset({1, 3, 5, 7, 9})


class TradingProxySession:
    """Holds the per-connection strategy/contract state for the trading proxy."""

    def __init__(
        self,
        *,
        allowed_client_requests: set[str],
        account_type: str = "demo",
        product_api: str = "options",
        user_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        self.allowed_client_requests = allowed_client_requests
        self.account_type = account_type.lower()
        self.product_api = product_api
        self.user_id = user_id
        self.account_id = account_id
        self.real_trading_confirmed = False
        self.real_consent_max_stake: float | None = None
        self.real_consent_max_daily_loss: float | None = None
        self.daily_realized_profit: float | None = None
        self.daily_profit_checked_at: float | None = None
        self.daily_profit_request_id: int | None = None
        # For real accounts, optimistically initialise P&L to 0 so the gate
        # does not block the very first trade while the risk monitor is fetching.
        # The monitor corrects the value within 15 seconds.
        if self.account_type == "real":
            self.daily_realized_profit = 0.0
            self.daily_profit_checked_at = time.time()
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
        # EVEN/ODD parity consecutive sequence tracker.
        # Stores the parity ("even"/"odd") of the last N completed ticks.
        self.parity_streak: list[str] = []
        self.parity_streak_max: int = 3
        self.even_odd_trigger_digits: list[int] = []
        self.even_odd_confirmation_deadline: int | None = None
        self.even_odd_recent_digits: list[int] = []
        self.even_odd_power_history: list[float] = []
        self.even_odd_cooldown_ticks: int = 0
        # When True, the next begin_signal call on a EVEN/ODD/MATCHES/DIFFERS
        # execution fires immediately without waiting for the M15/M5/M1 pipeline.
        self.scanner_direct_execute: bool = False

    def next_strategy_request_id(self) -> int:
        request_id = secrets.randbelow(1_000_000) + 9_000_000
        while (
            request_id in self.pending_strategy_proposals
            or request_id in self.pending_strategy_buys
            or request_id in self.strategy_request_deadlines
            or request_id in self.pending_settlement_checks
            or request_id == self.daily_profit_request_id
        ):
            request_id = secrets.randbelow(1_000_000) + 9_000_000
        return request_id

    def record_daily_profit_table(self, request_id: object, payload: object) -> bool:
        if type(request_id) is not int or request_id != self.daily_profit_request_id:
            return False
        table = payload.get("profit_table") if isinstance(payload, dict) else None
        transactions = table.get("transactions") if isinstance(table, dict) else None
        if not isinstance(transactions, list) or len(transactions) >= 5000:
            self.reject_daily_profit_request(request_id)
            return True

        profits: list[float] = []
        for transaction in transactions:
            raw_profit = transaction.get("profit") if isinstance(transaction, dict) else None
            if isinstance(raw_profit, bool):
                self.reject_daily_profit_request(request_id)
                return True
            try:
                profit = float(raw_profit)
            except (TypeError, ValueError, OverflowError):
                self.reject_daily_profit_request(request_id)
                return True
            if not math.isfinite(profit):
                self.reject_daily_profit_request(request_id)
                return True
            profits.append(profit)

        self.daily_realized_profit = sum(profits)
        self.daily_profit_checked_at = time.time()
        self.daily_profit_request_id = None
        return True

    def reject_daily_profit_request(self, request_id: object) -> None:
        if request_id == self.daily_profit_request_id:
            self.daily_realized_profit = None
            self.daily_profit_checked_at = None
            self.daily_profit_request_id = None

    def update_parity_streak(self, last_digit: int) -> str | None:
        """Record a tick's parity and return a signal if 3 consecutive same-parity ticks occurred.

        Signal path B — consecutive sequence:
        Three consecutive ODD digits → fire EVEN (reverse to opposite parity)
        Three consecutive EVEN digits → fire ODD

        Returns "DIGITEVEN" or "DIGITODD" when signal triggers, else None.
        """
        if last_digit in EVEN_DIGITS:
            parity = "even"
        elif last_digit in ODD_DIGITS:
            parity = "odd"
        else:
            return None

        self.parity_streak.append(parity)
        # Keep only the last N ticks
        if len(self.parity_streak) > self.parity_streak_max:
            self.parity_streak = self.parity_streak[-self.parity_streak_max:]

        if len(self.parity_streak) < self.parity_streak_max:
            return None

        # Check all same parity → fire the reverse
        if all(p == "odd" for p in self.parity_streak):
            self.parity_streak.clear()
            return "DIGITEVEN"
        if all(p == "even" for p in self.parity_streak):
            self.parity_streak.clear()
            return "DIGITODD"
        return None

    def update_even_odd_strategy(
        self,
        last_digit: int,
        percentages: dict[int, float],
        target_contract: str,
    ) -> str | None:
        """Advance the Maziwa rolling-window state machine."""
        target = ContractType.EVEN if target_contract == "DIGITEVEN" else ContractType.ODD
        if self.even_odd_cooldown_ticks > 0:
            self.even_odd_cooldown_ticks -= 1
            return None

        target_digits = EVEN_DIGITS if target is ContractType.EVEN else ODD_DIGITS
        parity_power = sum(float(percentages[digit]) for digit in target_digits)
        self.even_odd_power_history.append(parity_power)
        self.even_odd_power_history = self.even_odd_power_history[-6:]
        previous_power = (
            self.even_odd_power_history[0]
            if len(self.even_odd_power_history) >= 6
            else None
        )
        setup = evaluate_even_odd_setup(percentages, target, previous_power)
        if not setup.armed:
            self.even_odd_trigger_digits.clear()
            self.even_odd_confirmation_deadline = None
            return None

        if last_digit not in target_digits:
            self.even_odd_trigger_digits.append(last_digit)
            self.even_odd_trigger_digits = self.even_odd_trigger_digits[-4:]
            self.even_odd_confirmation_deadline = len(self.even_odd_trigger_digits)
            return None

        if len(self.even_odd_trigger_digits) >= 3:
            self.even_odd_trigger_digits.clear()
            self.even_odd_confirmation_deadline = None
            return target_contract
        self.even_odd_trigger_digits.clear()
        self.even_odd_confirmation_deadline = None
        return None

    def start_even_odd_cooldown(self) -> None:
        self.even_odd_cooldown_ticks = 120


    async def real_risk_monitor(self, upstream: Any) -> None:
        """Refresh server-trusted daily P/L; Real orders fail closed if it goes stale."""
        while True:
            request_id = self.next_strategy_request_id()
            self.daily_profit_request_id = request_id
            now = datetime.now(timezone.utc)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                await upstream.send(json.dumps({
                    "profit_table": 1,
                    "description": 1,
                    "limit": 5000,
                    "date_from": int(day_start.timestamp()),
                    "date_to": int(now.timestamp()),
                    "req_id": request_id,
                }, separators=(",", ":")))
            except Exception:
                self.daily_realized_profit = None
                self.daily_profit_checked_at = None
            await asyncio.sleep(15)

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
        self.parity_streak.clear()
        self.scanner_direct_execute = False

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
