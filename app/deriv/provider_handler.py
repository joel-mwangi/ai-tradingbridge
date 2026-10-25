"""Deriv-to-client message handler for the trading WebSocket proxy."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any

from fastapi import WebSocket

from app.strategy.execution import ExecutionState
from app.deriv.security import SYMBOL_PATTERN
from app.deriv.trading_session import TradingProxySession

_log = logging.getLogger(__name__)
_RETRIABLE_ERROR_CODES = {"RateLimit", "TemporaryError"}


async def handle_provider_messages(
    session: TradingProxySession,
    upstream: Any,
    websocket: WebSocket,
) -> None:
    """Read messages from Deriv upstream and forward/process them for the client."""
    _retry_counts: dict[str, int] = {}

    async for raw in upstream:
        try:
            provider_message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            provider_message = {}

        request_id = provider_message.get("req_id") if isinstance(provider_message, dict) else None

        # --- Strategy proposal/buy responses ---
        if session.strategy_execution is not None and isinstance(request_id, int):
            if request_id in session.pending_strategy_proposals:
                session.pending_strategy_proposals.discard(request_id)
                session.strategy_request_deadlines.pop(request_id, None)
                if provider_message.get("error"):
                    err_code = provider_message.get("error", {}).get("code", "")
                    if err_code in _RETRIABLE_ERROR_CODES:
                        retry_count = _retry_counts.get("proposal", 0)
                        if retry_count < 3:
                            _retry_counts["proposal"] = retry_count + 1
                            await asyncio.sleep(2 ** retry_count)
                            new_req_id = session.next_strategy_request_id()
                            session.strategy_execution.pending_proposal_request = new_req_id
                            session.strategy_execution.state = ExecutionState.WAITING_PROPOSAL
                            session.pending_strategy_proposals.add(new_req_id)
                            session.register_strategy_deadline(new_req_id, "proposal")
                            await upstream.send(json.dumps(session.strategy_execution._proposal(new_req_id), separators=(",", ":")))
                            continue
                    _retry_counts.pop("proposal", None)
                    session.strategy_execution.block(f"Deriv rejected the strategy proposal ({err_code}).")
                    await websocket.send_json(session.strategy_execution.status(f"Strategy execution blocked: proposal was rejected ({err_code})."))
                    await websocket.send_json(session.strategy_feed.unlock_execution())
                    session.strategy_execution.reset_for_next_signal()
                elif provider_message.get("msg_type") == "proposal":
                    _retry_counts.pop("proposal", None)
                    proposal = provider_message.get("proposal")
                    buy_request_id = session.next_strategy_request_id()
                    buy_message = session.strategy_execution.on_proposal(
                        request_id,
                        proposal if isinstance(proposal, dict) else {},
                        buy_request_id,
                    )
                    if buy_message is None:
                        await websocket.send_json(session.strategy_execution.status("Strategy execution blocked: invalid proposal response."))
                        await websocket.send_json(session.strategy_feed.unlock_execution())
                    else:
                        session.pending_strategy_buys.add(buy_request_id)
                        session.register_strategy_deadline(buy_request_id, "buy")
                        await upstream.send(json.dumps(buy_message, separators=(",", ":")))
                        await websocket.send_json(session.strategy_execution.status("Proposal received; submitting the next sequential buy."))

            elif request_id in session.pending_strategy_buys:
                session.pending_strategy_buys.discard(request_id)
                session.strategy_request_deadlines.pop(request_id, None)
                if provider_message.get("error"):
                    err_code = provider_message.get("error", {}).get("code", "")
                    if err_code in _RETRIABLE_ERROR_CODES:
                        retry_count = _retry_counts.get("buy", 0)
                        if retry_count < 3:
                            _retry_counts["buy"] = retry_count + 1
                            await asyncio.sleep(2 ** retry_count)
                            new_req_id = session.next_strategy_request_id()
                            session.strategy_execution.pending_proposal_request = new_req_id
                            session.strategy_execution.pending_buy_request = None
                            session.strategy_execution.state = ExecutionState.WAITING_PROPOSAL
                            session.pending_strategy_proposals.add(new_req_id)
                            session.register_strategy_deadline(new_req_id, "proposal")
                            await upstream.send(json.dumps(session.strategy_execution._proposal(new_req_id), separators=(",", ":")))
                            continue
                    _retry_counts.pop("buy", None)
                    session.strategy_execution.block(f"Deriv rejected the strategy buy ({err_code}).")
                    await websocket.send_json(session.strategy_execution.status(f"Strategy execution blocked: buy was rejected ({err_code})."))
                    await websocket.send_json(session.strategy_feed.unlock_execution())
                    session.strategy_execution.reset_for_next_signal()
                elif provider_message.get("msg_type") == "buy":
                    _retry_counts.pop("buy", None)
                    subscribe_message = session.strategy_execution.on_buy(request_id, provider_message)
                    if subscribe_message is None:
                        await websocket.send_json(session.strategy_execution.status("Strategy execution blocked: invalid buy response."))
                        await websocket.send_json(session.strategy_feed.unlock_execution())
                    else:
                        session.active_poc_sub_id = None
                        await upstream.send(json.dumps(subscribe_message, separators=(",", ":")))
                        await websocket.send_json(session.strategy_execution.status("Buy accepted; waiting for contract settlement before continuing."))

        # --- Contract settlement ---
        if session.strategy_execution is not None and provider_message.get("msg_type") == "proposal_open_contract":
            contract = provider_message.get("proposal_open_contract")
            if isinstance(contract, dict):
                if session.active_poc_sub_id is None:
                    sub_info = provider_message.get("subscription") or contract.get("subscription")
                    if isinstance(sub_info, dict):
                        session.active_poc_sub_id = sub_info.get("id")
                    elif isinstance(contract.get("id"), str):
                        session.active_poc_sub_id = contract.get("id")

                contract_id = contract.get("contract_id")
                contract_status = contract.get("status")
                # Settle only on definitive terminal states with is_sold confirmed.
                # "sold" status can appear as an intermediate message before profit
                # is populated — wait for "won"/"lost" or explicit is_sold=1.
                is_sold_flag = contract.get("is_sold") in (1, True, "1")
                terminal_status = contract_status in {"won", "lost"}
                settled = is_sold_flag and (terminal_status or contract.get("profit") is not None)
                if settled and isinstance(contract_id, (str, int)):
                    if session.active_poc_sub_id:
                        try:
                            await upstream.send(json.dumps({"forget": session.active_poc_sub_id}, separators=(",", ":")))
                        except Exception:
                            pass
                        session.active_poc_sub_id = None

                    next_request_id = session.next_strategy_request_id()
                    next_proposal = session.strategy_execution.on_settlement(
                        str(contract_id),
                        contract_status if isinstance(contract_status, str) else None,
                        contract.get("profit"),
                        next_request_id,
                        confirmed_settled=True,
                    )
                    if next_proposal is not None:
                        session.pending_strategy_proposals.add(next_request_id)
                        session.register_strategy_deadline(next_request_id, "proposal")
                        await upstream.send(json.dumps(next_proposal, separators=(",", ":")))
                        await websocket.send_json(session.strategy_execution.status("Contract settled; requesting the next sequential buy."))
                    elif session.strategy_execution.state is ExecutionState.STOPPED:
                        await websocket.send_json(session.strategy_execution.status("Two consecutive losses reached; no further buys will be submitted."))
                    elif session.strategy_execution.state is ExecutionState.COMPLETED:
                        await websocket.send_json(session.strategy_execution.status("Three sequential buys completed; strategy execution stopped."))
                    if session.strategy_execution.state in (ExecutionState.STOPPED, ExecutionState.COMPLETED):
                        await websocket.send_json(session.strategy_feed.unlock_execution())
                        await websocket.send_json(session.strategy_feed.stop())
                        session.strategy_execution.reset_for_next_signal()

        # --- History seeding ---
        if provider_message.get("msg_type") == "history":
            history_request_id = provider_message.get("req_id")
            history_symbol = session.strategy_history_requests.pop(history_request_id, None) if isinstance(history_request_id, int) else None
            history = provider_message.get("history")
            if history_symbol and isinstance(history, dict):
                pip_size = provider_message.get("pip_size")
                if isinstance(pip_size, int) and not isinstance(pip_size, bool) and 0 <= pip_size <= 12:
                    session.strategy_pip_size = pip_size
                session.strategy_frequency.seed_history(
                    history.get("prices") or [],
                    history.get("times") or [],
                    session.strategy_pip_size,
                )
                provider_message["digit_frequency"] = session.strategy_frequency.snapshot(history_symbol, time.time())

        # --- Live tick routing ---
        if session.strategy_execution is not None and provider_message.get("msg_type") == "tick":
            tick = provider_message.get("tick")
            if isinstance(tick, dict):
                symbol = tick.get("symbol") or tick.get("underlying_symbol")
                if not symbol:
                    _log.debug("trading proxy tick missing 'symbol' field: %r", tick)
                if symbol == session.strategy_subscribed_symbol:
                    pip_size = tick.get("pip_size") or provider_message.get("pip_size")
                    if isinstance(pip_size, int) and not isinstance(pip_size, bool) and 0 <= pip_size <= 12:
                        session.strategy_pip_size = pip_size
                    tick_result = session.strategy_frequency.add_tick(
                        tick.get("quote"), tick.get("epoch"), session.strategy_pip_size, None,
                    )
                    provider_message["digit_frequency"] = session.strategy_frequency.snapshot(symbol, time.time())
                    active_strategy = (
                        session.strategy_feed.session is not None
                        and session.strategy_feed.session.state.value not in {"idle", "stopped", "blocked"}
                    )
                    if active_strategy and tick_result not in {"accepted", "history_overlap"}:
                        tick_status = session.strategy_feed.reject_tick(
                            "Duplicate live tick detected."
                            if tick_result == "duplicate"
                            else "Invalid or out-of-order live tick detected."
                        )
                        if tick_status:
                            await websocket.send_json(tick_status)
                    else:
                        was_selected = bool(
                            session.strategy_feed.session
                            and session.strategy_feed.session.state.value == "selected"
                        )
                        strategy_status = session.strategy_feed.on_frequency(provider_message["digit_frequency"])
                        if strategy_status:
                            await websocket.send_json(strategy_status)
                        if was_selected:
                            tick_status = session.strategy_feed.on_tick(symbol, tick, session.strategy_pip_size)
                            if tick_status:
                                await websocket.send_json(tick_status)
                                if tick_status.get("signal_generated") and session.strategy_execution.state is ExecutionState.IDLE:
                                    proposal_request_id = session.next_strategy_request_id()
                                    proposal_message = session.strategy_execution.begin_signal(proposal_request_id)
                                    if proposal_message is not None:
                                        session.pending_strategy_proposals.add(proposal_request_id)
                                        session.register_strategy_deadline(proposal_request_id, "proposal")
                                        session.strategy_feed.lock_execution()
                                        await upstream.send(json.dumps(proposal_message, separators=(",", ":")))
                                        await websocket.send_json(session.strategy_execution.status("Confirmed signal received; requesting the first automatic buy."))

        # --- Manual proposal caching ---
        if isinstance(request_id, int) and request_id in session.pending_proposal_requests:
            if provider_message.get("msg_type") == "proposal":
                proposal = provider_message.get("proposal")
                if isinstance(proposal, dict):
                    proposal_id = proposal.get("id")
                    try:
                        ask_price = float(proposal.get("ask_price"))
                    except (TypeError, ValueError):
                        ask_price = 0
                    if isinstance(proposal_id, (str, int)) and str(proposal_id) and ask_price > 0 and math.isfinite(ask_price):
                        session.valid_proposals[str(proposal_id)] = ask_price
                        while len(session.valid_proposals) > 128:
                            session.valid_proposals.pop(next(iter(session.valid_proposals)))
            if provider_message.get("error") or provider_message.get("msg_type") == "proposal":
                session.pending_proposal_requests.discard(request_id)

        await websocket.send_text(raw)
