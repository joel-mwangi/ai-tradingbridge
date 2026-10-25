"""Client-to-Deriv message handler for the trading WebSocket proxy."""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import WebSocket

from app.market_analysis import DigitFrequencyWindow
from app.strategy.digit_strategy import Group
from app.strategy.execution import StrategyExecution
from app.deriv.security import SYMBOL_PATTERN
from app.deriv.trading_session import TradingProxySession


async def handle_client_messages(
    session: TradingProxySession,
    upstream: Any,
    websocket: WebSocket,
) -> None:
    """Read messages from the client, validate them, and forward to Deriv upstream."""
    while True:
        raw = await websocket.receive_text()
        if len(raw) > 256_000:
            await websocket.close(code=4400)
            return
        try:
            message = json.loads(raw)
        except ValueError:
            await websocket.send_json({"error": {"code": "INVALID_JSON", "message": "Send a valid JSON request."}})
            continue
        if not isinstance(message, dict):
            await websocket.send_json({"error": {"code": "UNSUPPORTED_REQUEST", "message": "Send a single Deriv request object."}})
            continue

        if isinstance(message.get("strategy"), dict):
            await _handle_strategy_control(session, upstream, websocket, message["strategy"])
            continue

        request_keys = set(message).intersection(session.allowed_client_requests)
        if len(request_keys) != 1 or "authorize" in message:
            await websocket.send_json({"error": {"code": "UNSUPPORTED_REQUEST", "message": "Send exactly one supported Deriv request."}})
            continue

        if "proposal" in message:
            if not await _validate_proposal(session, websocket, message):
                continue

        if "buy" in message:
            if not await _validate_buy(session, websocket, message):
                continue

        if "sell" in message:
            if not str(message.get("sell", "")).isdigit() or message.get("price") != 0:
                await websocket.send_json({"error": {"code": "INVALID_SELL", "message": "Only a market sale of an open contract is supported."}})
                continue

        if "proposal" in message:
            session.pending_proposal_requests.add(message["req_id"])
        await upstream.send(json.dumps(message, separators=(",", ":")))


async def _handle_strategy_control(
    session: TradingProxySession,
    upstream: Any,
    websocket: WebSocket,
    control: dict,
) -> None:
    action = control.get("action")
    if action == "start":
        await _handle_strategy_start(session, upstream, websocket, control)
    elif action == "stop":
        strategy_status = session.strategy_feed.stop()
        if session.strategy_execution is not None:
            session.strategy_execution.stop()
            await websocket.send_json(session.strategy_execution.status("Strategy execution stopped by the user."))
        await websocket.send_json(strategy_status)
    elif action == "status":
        await websocket.send_json(session.strategy_feed._status("Current strategy observation status."))
        if session.strategy_execution is not None:
            await websocket.send_json(session.strategy_execution.status("Current strategy execution status."))
    else:
        await websocket.send_json({"error": {"code": "INVALID_STRATEGY_ACTION", "message": "Supported strategy actions are start, stop, and status."}})


async def _handle_strategy_start(
    session: TradingProxySession,
    upstream: Any,
    websocket: WebSocket,
    control: dict,
) -> None:
    symbol = control.get("symbol")
    window_size = control.get("window_size", 1000)
    barrier = control.get("barrier", 5)
    direction = control.get("direction")

    if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
        await websocket.send_json({"error": {"code": "INVALID_STRATEGY_SYMBOL", "message": "Strategy start requires a valid market symbol."}})
        return
    if type(window_size) is not int or window_size not in (50, 100, 250, 500, 1000):
        await websocket.send_json({"error": {"code": "INVALID_STRATEGY_WINDOW", "message": "Strategy window_size must be 50, 100, 250, 500, or 1000 ticks."}})
        return
    if type(barrier) is not int or barrier not in range(10):
        await websocket.send_json({"error": {"code": "INVALID_STRATEGY_BARRIER", "message": "Strategy barrier must be an integer digit from 0 to 9."}})
        return
    if direction not in ("over", "under"):
        await websocket.send_json({"error": {"code": "INVALID_STRATEGY_DIRECTION", "message": "Strategy start requires direction 'over' or 'under'."}})
        return

    try:
        stake = float(control.get("stake", os.getenv("TRADEBRIDGE_STRATEGY_STAKE", "1")))
        duration = control.get("duration", 1)
        currency = control.get("currency", os.getenv("TRADEBRIDGE_STRATEGY_CURRENCY", "USD"))
        group = Group(direction)
        candidate_execution = StrategyExecution(
            symbol, group, barrier, stake=stake, currency=currency, duration=duration,
        )
        if stake > float(os.getenv("TRADEBRIDGE_DEMO_MAX_STAKE", "1000")):
            raise ValueError("Strategy stake exceeds the configured demo limit.")
    except (TypeError, ValueError) as exc:
        await websocket.send_json({"error": {"code": "STRATEGY_CONFIG", "message": str(exc)}})
        return

    try:
        strategy_status = session.strategy_feed.start(
            symbol, window_size=window_size, barrier=barrier, direction=direction,
        )
    except ValueError as exc:
        await websocket.send_json({"error": {"code": "STRATEGY_CONFIG", "message": str(exc)}})
        return

    session.strategy_execution = candidate_execution
    session.strategy_frequency = DigitFrequencyWindow()
    session.strategy_pip_size = None
    session.strategy_subscribed_symbol = symbol
    session.strategy_window_size = window_size
    history_request_id = session.next_strategy_request_id()
    session.strategy_history_requests[history_request_id] = symbol
    await upstream.send(json.dumps({
        "ticks_history": symbol,
        "count": window_size,
        "end": "latest",
        "style": "ticks",
        "req_id": history_request_id,
    }, separators=(",", ":")))
    await upstream.send(json.dumps({"ticks": symbol, "subscribe": 1}, separators=(",", ":")))
    await websocket.send_json(strategy_status)
    await websocket.send_json(session.strategy_execution.status("Strategy execution armed for the next confirmed signal."))


async def _validate_proposal(
    session: TradingProxySession,
    websocket: WebSocket,
    message: dict,
) -> bool:
    amount = message.get("amount")
    contract_type = message.get("contract_type")
    duration = message.get("duration")
    duration_unit = message.get("duration_unit")
    symbol = message.get("underlying_symbol", message.get("symbol"))
    allowed_contracts = {"DIGITOVER", "DIGITUNDER", "DIGITEVEN", "DIGITODD", "DIGITMATCH", "DIGITDIFF"}
    try:
        stake = float(amount)
    except (TypeError, ValueError):
        stake = 0
    if not (1 <= stake <= float(os.getenv("TRADEBRIDGE_DEMO_MAX_STAKE", "1000"))):
        await websocket.send_json({"error": {"code": "STAKE_LIMIT", "message": "Demo stake is outside the server-configured limit."}})
        return False
    request_id = message.get("req_id")
    if type(request_id) is not int or request_id < 0:
        await websocket.send_json({"error": {"code": "INVALID_REQUEST_ID", "message": "A valid request ID is required for a proposal."}})
        return False
    if contract_type not in allowed_contracts or not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
        await websocket.send_json({"error": {"code": "INVALID_CONTRACT", "message": "Only supported digit contracts and valid symbols are allowed."}})
        return False
    if duration_unit != "t" or type(duration) is not int or not 1 <= duration <= 10:
        await websocket.send_json({"error": {"code": "INVALID_DURATION", "message": "Digit contracts must use 1–10 ticks."}})
        return False
    if contract_type in {"DIGITOVER", "DIGITUNDER", "DIGITMATCH", "DIGITDIFF"}:
        barrier = message.get("barrier")
        if not isinstance(barrier, (str, int)) or not str(barrier).isdigit() or not 0 <= int(barrier) <= 9:
            await websocket.send_json({"error": {"code": "INVALID_BARRIER", "message": "A valid digit barrier is required."}})
            return False
        if (contract_type == "DIGITOVER" and int(barrier) > 8) or (contract_type == "DIGITUNDER" and int(barrier) < 1):
            await websocket.send_json({"error": {"code": "INVALID_BARRIER", "message": "The barrier is outside the contract's valid range."}})
            return False
    if contract_type in {"DIGITEVEN", "DIGITODD"} and "barrier" in message:
        await websocket.send_json({"error": {"code": "INVALID_BARRIER", "message": "Even/Odd contracts must not include a barrier."}})
        return False
    return True


async def _validate_buy(
    session: TradingProxySession,
    websocket: WebSocket,
    message: dict,
) -> bool:
    try:
        buy_price = float(message.get("price"))
    except (TypeError, ValueError):
        buy_price = 0
    if not (0 < buy_price <= float(os.getenv("TRADEBRIDGE_DEMO_MAX_STAKE", "1000"))):
        await websocket.send_json({"error": {"code": "STAKE_LIMIT", "message": "Purchase price is outside the server-configured limit."}})
        return False
    if not isinstance(message.get("buy"), (str, int)) or not str(message.get("buy")).strip():
        await websocket.send_json({"error": {"code": "INVALID_PROPOSAL", "message": "A valid proposal identifier is required."}})
        return False
    proposal_id = str(message.get("buy"))
    quoted_price = session.valid_proposals.get(proposal_id)
    if quoted_price is None:
        await websocket.send_json({"error": {"code": "INVALID_PROPOSAL", "message": "Purchase must use a fresh proposal created on this session."}})
        return False
    if buy_price + 1e-9 < quoted_price:
        await websocket.send_json({"error": {"code": "INVALID_PURCHASE_PRICE", "message": "Purchase maximum price cannot be below the quoted ask price."}})
        return False
    session.valid_proposals.pop(proposal_id, None)
    return True
