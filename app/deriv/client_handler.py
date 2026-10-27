"""Client-to-Deriv message handler for the trading WebSocket proxy."""
from __future__ import annotations

import time

import json
import math
import os
from typing import Any

from fastapi import WebSocket

from app.market_analysis import DigitFrequencyWindow
from app.strategy.digit_strategy import Group
from app.strategy.execution import StrategyExecution
from app.deriv.security import SYMBOL_PATTERN
from app.deriv.trading_session import TradingProxySession

REAL_TRADING_CONFIRMATION = "ENABLE REAL TRADING"


def _configured_stake_limit(session: TradingProxySession) -> float | None:
    variable = "TRADEBRIDGE_REAL_MAX_STAKE" if session.account_type == "real" else "TRADEBRIDGE_DEMO_MAX_STAKE"
    raw_limit = os.getenv(variable)
    if raw_limit is None:
        if session.account_type == "real":
            return None
        raw_limit = "1000"
    try:
        limit = float(raw_limit)
    except (TypeError, ValueError):
        return None
    return limit if math.isfinite(limit) and limit > 0 else None


def _real_trading_gate_error(session: TradingProxySession) -> dict[str, str] | None:
    if session.account_type != "real":
        return None
    if os.getenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "false").strip().lower() != "true":
        return {
            "code": "REAL_TRADING_DISABLED",
            "message": "Real trading is disabled by server configuration.",
        }
    if _configured_stake_limit(session) is None:
        return {
            "code": "REAL_STAKE_LIMIT_NOT_CONFIGURED",
            "message": "Set a positive finite TRADEBRIDGE_REAL_MAX_STAKE before enabling Real trading.",
        }
    if not session.real_trading_confirmed:
        return {
            "code": "REAL_TRADING_CONFIRMATION_REQUIRED",
            "message": "Confirm Real trading on this authenticated WebSocket connection before trading.",
        }
    return None


def _stake_limit_error(session: TradingProxySession) -> str | None:
    if _configured_stake_limit(session) is None:
        variable = "TRADEBRIDGE_REAL_MAX_STAKE" if session.account_type == "real" else "TRADEBRIDGE_DEMO_MAX_STAKE"
        return f"A positive finite {variable} must be configured."
    return None


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

        if isinstance(message.get("real_trading"), dict):
            await _handle_real_trading_control(session, websocket, message["real_trading"])
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


async def _handle_real_trading_control(
    session: TradingProxySession,
    websocket: WebSocket,
    control: dict,
) -> None:
    if control.get("action") != "confirm":
        await websocket.send_json({"error": {"code": "INVALID_REAL_TRADING_ACTION", "message": "Supported Real trading action is confirm."}})
        return
    if session.account_type != "real":
        await websocket.send_json({"error": {"code": "REAL_ACCOUNT_REQUIRED", "message": "Real trading confirmation is only available on a Real account connection."}})
        return
    if control.get("confirmation") != REAL_TRADING_CONFIRMATION:
        await websocket.send_json({"error": {"code": "REAL_TRADING_CONFIRMATION_REQUIRED", "message": f"Send the exact confirmation phrase: {REAL_TRADING_CONFIRMATION}."}})
        return
    if os.getenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "false").strip().lower() != "true":
        await websocket.send_json({"error": _real_trading_gate_error(session)})
        return
    if _configured_stake_limit(session) is None:
        await websocket.send_json({"error": _real_trading_gate_error(session)})
        return

    session.real_trading_confirmed = True
    await websocket.send_json({
        "type": "real_trading_status",
        "enabled": True,
        "account_type": "real",
        "message": "Real trading confirmed for this WebSocket connection; confirmation expires when it disconnects.",
    })


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
    real_trading_error = _real_trading_gate_error(session)
    if real_trading_error:
        await websocket.send_json({"error": real_trading_error})
        return

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
        if stake <= 0:
            raise ValueError("Strategy stake must be a positive amount.")
        max_stake = _configured_stake_limit(session)
        if max_stake is None:
            raise ValueError(_stake_limit_error(session))
        if stake > max_stake:
            limit_name = "real" if session.account_type == "real" else "demo"
            raise ValueError(f"Strategy stake exceeds the configured {limit_name} limit.")
    except (TypeError, ValueError) as exc:
        await websocket.send_json({"error": {"code": "STRATEGY_CONFIG", "message": str(exc)}})
        return

    already_subscribed = (session.strategy_subscribed_symbol == symbol)
    initial_frequency = (
        session.strategy_frequency.snapshot(symbol, time.time())
        if already_subscribed and session.strategy_frequency.points
        else None
    )

    try:
        strategy_status = session.strategy_feed.start(
            symbol,
            window_size=window_size,
            barrier=barrier,
            direction=direction,
            initial_frequency=initial_frequency,
        )
    except ValueError as exc:
        await websocket.send_json({"error": {"code": "STRATEGY_CONFIG", "message": str(exc)}})
        return

    session.strategy_execution = candidate_execution
    session.strategy_window_size = window_size

    if not already_subscribed:
        if session.strategy_subscribed_symbol is not None:
            try:
                await upstream.send(json.dumps({"forget_all": "ticks"}, separators=(",", ":")))
            except Exception:
                pass
        session.strategy_frequency = DigitFrequencyWindow()
        session.strategy_pip_size = None
        session.strategy_subscribed_symbol = symbol
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
    else:
        # Keep warm frequency tracker and avoid creating duplicate upstream
        # tick subscriptions when Start is run again on the same symbol.
        if len(session.strategy_frequency.points) < window_size:
            history_request_id = session.next_strategy_request_id()
            session.strategy_history_requests[history_request_id] = symbol
            await upstream.send(json.dumps({
                "ticks_history": symbol,
                "count": window_size,
                "end": "latest",
                "style": "ticks",
                "req_id": history_request_id,
            }, separators=(",", ":")))

    await websocket.send_json(strategy_status)
    await websocket.send_json(session.strategy_execution.status("Strategy execution armed for the next confirmed signal."))


async def _validate_proposal(
    session: TradingProxySession,
    websocket: WebSocket,
    message: dict,
) -> bool:
    real_trading_error = _real_trading_gate_error(session)
    if real_trading_error:
        await websocket.send_json({"error": real_trading_error})
        return False

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
    max_stake = _configured_stake_limit(session)
    if max_stake is None:
        await websocket.send_json({"error": {"code": "STAKE_LIMIT_NOT_CONFIGURED", "message": _stake_limit_error(session)}})
        return False
    if not (0 < stake <= max_stake):
        limit_name = "Real" if session.account_type == "real" else "Demo"
        await websocket.send_json({"error": {"code": "STAKE_LIMIT", "message": f"{limit_name} stake is outside the server-configured limit."}})
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
    real_trading_error = _real_trading_gate_error(session)
    if real_trading_error:
        await websocket.send_json({"error": real_trading_error})
        return False

    try:
        buy_price = float(message.get("price"))
    except (TypeError, ValueError):
        buy_price = 0
    max_stake = _configured_stake_limit(session)
    if max_stake is None:
        await websocket.send_json({"error": {"code": "STAKE_LIMIT_NOT_CONFIGURED", "message": _stake_limit_error(session)}})
        return False
    if not (0 < buy_price <= max_stake):
        limit_name = "Real" if session.account_type == "real" else "Demo"
        await websocket.send_json({"error": {"code": "STAKE_LIMIT", "message": f"{limit_name} purchase price is outside the server-configured limit."}})
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
