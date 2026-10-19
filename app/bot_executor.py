"""Inline Demo bot executor.

Drives a single bot run over an already-authenticated Deriv WebSocket.
No database I/O — all state is in-memory and streamed to the caller via send_event.

The executor uses bot_state.TradeSnapshot / transition() for every state change so
every trade follows the fail-closed state machine: CREATED → REQUESTING_PROPOSAL →
BUY_PENDING → OPEN → SETTLING → SETTLED (or FAILED / OUTCOME_UNKNOWN on error).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import websockets.legacy.client as ws_client

from app.bot_schema import DemoBotConfig
from app.bot_state import TradeSnapshot, TradeState, may_start_next_trade, transition
from app.contract_normalization import is_terminal_contract, normalize_settlement, parse_deriv_number


@dataclass
class BotResult:
    stop_reason: str
    trade_count: int
    total_profit: float


async def run_bot(
    config: DemoBotConfig,
    upstream: ws_client.WebSocketClientProtocol,
    send_event: Callable[[dict], Awaitable[None]],
    stop_signal: asyncio.Event,
) -> BotResult:
    """Run the bot loop until a stop condition is met or stop_signal is set.

    Args:
        config: Validated DemoBotConfig (symbol, contract, stake, stop conditions).
        upstream: Open authenticated Deriv WebSocket connection (demo account OTP session).
        send_event: Async callback that sends a JSON-serialisable dict to the frontend.
        stop_signal: Set this event externally to request a graceful stop after the
                     current trade completes.

    Returns:
        BotResult with stop_reason, final trade_count, and total_profit.
    """
    trade_count = 0
    total_profit = 0.0
    consecutive_losses = 0
    start_time = time.monotonic()
    req_id_counter = 1
    stop_reason = "unknown"

    # Futures waiting for a specific req_id response from Deriv.
    pending: dict[int, asyncio.Future[dict]] = {}
    # Futures waiting for a specific contract_id to settle.
    contract_futures: dict[str, asyncio.Future[dict]] = {}

    loop = asyncio.get_event_loop()

    async def _reader() -> None:
        """Background coroutine: route every Deriv message to the right waiting future."""
        try:
            async for raw in upstream:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue

                # Route by req_id → pending proposal/buy/subscribe futures.
                rid = msg.get("req_id")
                if isinstance(rid, int) and rid in pending:
                    fut = pending[rid]
                    if not fut.done():
                        fut.set_result(msg)

                # Route proposal_open_contract by contract_id → settlement futures.
                if msg.get("msg_type") == "proposal_open_contract":
                    poc = msg.get("proposal_open_contract") or {}
                    if isinstance(poc, dict):
                        cid = str(poc.get("contract_id", ""))
                        if cid and cid in contract_futures:
                            fut = contract_futures[cid]
                            if not fut.done():
                                # Resolve only on a provider-confirmed terminal contract state.
                                if is_terminal_contract(poc):
                                    fut.set_result(msg)
        except Exception:
            pass
        finally:
            # Wake any still-waiting futures so the main loop doesn't hang.
            err = ConnectionError("Deriv connection closed")
            for fut in list(pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            for fut in list(contract_futures.values()):
                if not fut.done():
                    fut.set_exception(err)

    reader_task = asyncio.create_task(_reader())

    try:
        await send_event({"event": "started", "config": config.model_dump()})

        while not stop_signal.is_set():
            # --- Pre-trade stop condition checks ---
            elapsed = time.monotonic() - start_time
            for sc in config.stopConditions:
                if sc.type == "time" and sc.seconds is not None and elapsed >= sc.seconds:
                    stop_reason = "time_limit"
                    return BotResult(stop_reason, trade_count, total_profit)

            if trade_count >= config.maxTradesPerDay:
                stop_reason = "max_trades_per_day"
                return BotResult(stop_reason, trade_count, total_profit)

            # ── STEP 1: Request a proposal ───────────────────────────────────────
            snap = TradeSnapshot(state=TradeState.CREATED)
            snap = transition(snap, TradeState.REQUESTING_PROPOSAL)

            req_id = req_id_counter
            req_id_counter += 1
            proposal_future: asyncio.Future[dict] = loop.create_future()
            pending[req_id] = proposal_future

            proposal_msg: dict = {
                "proposal": 1,
                "amount": config.stakeStrategy.amount,
                "basis": "stake",
                "contract_type": config.contractType,
                "currency": "USD",
                "duration": config.duration,
                "duration_unit": config.durationUnit,
                "symbol": config.symbol,
                "req_id": req_id,
            }
            if config.barrier is not None:
                proposal_msg["barrier"] = config.barrier

            await upstream.send(json.dumps(proposal_msg, separators=(",", ":")))

            try:
                proposal_response = await asyncio.wait_for(
                    proposal_future, timeout=15.0
                )
            except asyncio.TimeoutError:
                pending.pop(req_id, None)
                snap = transition(snap, TradeState.FAILED)
                await send_event({"event": "error", "reason": "proposal_timeout"})
                stop_reason = "proposal_timeout"
                return BotResult(stop_reason, trade_count, total_profit)
            except ConnectionError:
                snap = transition(snap, TradeState.FAILED)
                await send_event({"event": "error", "reason": "connection_closed"})
                stop_reason = "connection_closed"
                return BotResult(stop_reason, trade_count, total_profit)
            finally:
                pending.pop(req_id, None)

            if proposal_response.get("error"):
                snap = transition(snap, TradeState.FAILED)
                err = proposal_response["error"]
                detail = err.get("message", "Proposal error") if isinstance(err, dict) else "Proposal error"
                await send_event({"event": "error", "reason": "proposal_error", "detail": detail})
                stop_reason = "proposal_error"
                return BotResult(stop_reason, trade_count, total_profit)

            proposal = proposal_response.get("proposal") or {}
            proposal_id = str(proposal.get("id", ""))
            try:
                ask_price = float(proposal.get("ask_price", 0))
            except (TypeError, ValueError):
                ask_price = 0.0

            if not proposal_id or ask_price <= 0:
                snap = transition(snap, TradeState.FAILED)
                await send_event({"event": "error", "reason": "invalid_proposal_response"})
                stop_reason = "invalid_proposal_response"
                return BotResult(stop_reason, trade_count, total_profit)

            snap = transition(snap, TradeState.BUY_PENDING, proposal_id=proposal_id, stake=ask_price)

            # ── STEP 2: Send buy ─────────────────────────────────────────────────
            buy_req_id = req_id_counter
            req_id_counter += 1
            buy_future: asyncio.Future[dict] = loop.create_future()
            pending[buy_req_id] = buy_future

            await upstream.send(json.dumps({
                "buy": proposal_id,
                "price": ask_price,
                "req_id": buy_req_id,
            }, separators=(",", ":")))

            try:
                buy_response = await asyncio.wait_for(
                    buy_future, timeout=15.0
                )
            except asyncio.TimeoutError:
                pending.pop(buy_req_id, None)
                snap = transition(snap, TradeState.OUTCOME_UNKNOWN)
                await send_event({"event": "error", "reason": "buy_timeout", "state": "outcome_unknown"})
                stop_reason = "buy_timeout"
                return BotResult(stop_reason, trade_count, total_profit)
            except ConnectionError:
                snap = transition(snap, TradeState.OUTCOME_UNKNOWN)
                await send_event({"event": "error", "reason": "connection_closed", "state": "outcome_unknown"})
                stop_reason = "connection_closed"
                return BotResult(stop_reason, trade_count, total_profit)
            finally:
                pending.pop(buy_req_id, None)

            if buy_response.get("error"):
                snap = transition(snap, TradeState.FAILED)
                err = buy_response["error"]
                detail = err.get("message", "Buy error") if isinstance(err, dict) else "Buy error"
                await send_event({"event": "error", "reason": "buy_error", "detail": detail})
                stop_reason = "buy_error"
                return BotResult(stop_reason, trade_count, total_profit)

            buy_data = buy_response.get("buy") or {}
            contract_id = str(buy_data.get("contract_id", ""))
            if not contract_id:
                snap = transition(snap, TradeState.OUTCOME_UNKNOWN)
                await send_event({"event": "error", "reason": "missing_contract_id", "state": "outcome_unknown"})
                stop_reason = "missing_contract_id"
                return BotResult(stop_reason, trade_count, total_profit)

            # Prefer the broker-confirmed purchase price over the proposal quote.
            actual_stake = parse_deriv_number(buy_data.get("buy_price"))
            if actual_stake is None or actual_stake <= 0:
                actual_stake = ask_price

            snap = transition(snap, TradeState.OPEN, contract_id=contract_id, stake=actual_stake)
            await send_event({"event": "trade_opened", "contract_id": contract_id, "stake": actual_stake})

            # ── STEP 3: Subscribe to contract updates and wait for settlement ────
            contract_future: asyncio.Future[dict] = loop.create_future()
            contract_futures[contract_id] = contract_future

            sub_req_id = req_id_counter
            req_id_counter += 1
            # contract_id from Deriv is numeric; send as int if possible.
            cid_value: int | str = int(contract_id) if contract_id.isdigit() else contract_id
            await upstream.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": cid_value,
                "subscribe": 1,
                "req_id": sub_req_id,
            }, separators=(",", ":")))

            snap = transition(snap, TradeState.SETTLING)

            try:
                settlement_msg = await asyncio.wait_for(
                    contract_future, timeout=60.0
                )
            except asyncio.TimeoutError:
                contract_futures.pop(contract_id, None)
                snap = transition(snap, TradeState.OUTCOME_UNKNOWN)
                await send_event({"event": "error", "reason": "settlement_timeout", "state": "outcome_unknown"})
                stop_reason = "settlement_timeout"
                return BotResult(stop_reason, trade_count, total_profit)
            except ConnectionError:
                contract_futures.pop(contract_id, None)
                snap = transition(snap, TradeState.OUTCOME_UNKNOWN)
                await send_event({"event": "error", "reason": "connection_closed", "state": "outcome_unknown"})
                stop_reason = "connection_closed"
                return BotResult(stop_reason, trade_count, total_profit)
            finally:
                contract_futures.pop(contract_id, None)

            poc = settlement_msg.get("proposal_open_contract") or {}
            settlement = normalize_settlement(poc, stake=actual_stake)
            if settlement is None:
                # Do not invent zero values or advance the bot after an ambiguous
                # terminal response. A later reconciliation must establish the result.
                snap = transition(snap, TradeState.OUTCOME_UNKNOWN)
                await send_event({
                    "event": "error",
                    "reason": "settlement_values_missing",
                    "state": "outcome_unknown",
                    "contract_id": contract_id,
                })
                stop_reason = "settlement_values_missing"
                return BotResult(stop_reason, trade_count, total_profit)

            payout = settlement["payout"]
            profit = settlement["profit"]
            snap = transition(snap, TradeState.SETTLED, payout=payout, profit=profit)
            assert may_start_next_trade(snap), "State machine invariant violated after SETTLED"

            trade_count += 1
            total_profit = round(total_profit + profit, 8)
            consecutive_losses = 0 if profit >= 0 else consecutive_losses + 1

            await send_event({
                "event": "trade_settled",
                "contract_id": contract_id,
                "stake": actual_stake,
                "payout": payout,
                "profit": profit,
                "outcome": settlement["outcome"],
                "provider_status": settlement["provider_status"],
                "trade_count": trade_count,
                "total_profit": total_profit,
                "consecutive_losses": consecutive_losses,
            })

            # ── STEP 4: Check stop conditions after every settled trade ──────────
            for sc in config.stopConditions:
                if sc.type == "profit" and sc.target is not None and total_profit >= sc.target:
                    stop_reason = "profit_target"
                    return BotResult(stop_reason, trade_count, total_profit)
                if sc.type == "loss" and sc.limit is not None and total_profit <= -sc.limit:
                    stop_reason = "loss_limit"
                    return BotResult(stop_reason, trade_count, total_profit)
                if sc.type == "tradeCount" and sc.max is not None and trade_count >= sc.max:
                    stop_reason = "trade_count"
                    return BotResult(stop_reason, trade_count, total_profit)

            if consecutive_losses >= config.maxConsecutiveLosses:
                stop_reason = "max_consecutive_losses"
                return BotResult(stop_reason, trade_count, total_profit)

            if stop_signal.is_set():
                stop_reason = "user_requested"
                return BotResult(stop_reason, trade_count, total_profit)

            # ── STEP 5: Cooldown between trades ──────────────────────────────────
            # Use asyncio.wait_for on stop_signal.wait() so a stop request during
            # the cooldown period exits immediately rather than sleeping the full duration.
            try:
                await asyncio.wait_for(
                    stop_signal.wait(),
                    timeout=float(config.cooldownSeconds),
                )
                # stop_signal was set during cooldown — exit cleanly.
                stop_reason = "user_requested"
                return BotResult(stop_reason, trade_count, total_profit)
            except asyncio.TimeoutError:
                pass  # Normal cooldown expiry; continue to next trade.

        stop_reason = "user_requested"
        return BotResult(stop_reason, trade_count, total_profit)

    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
