"""Read-only Deriv proposal quotes for Even/Odd contracts.

This module requests prices only. It never sends buy, sell, or contract
management messages.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import websockets


DERIV_PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{3,20}$")
CONTRACT_TYPES = {"even": "DIGITEVEN", "odd": "DIGITODD"}


class PayoutQuoteError(RuntimeError):
    """Raised when Deriv cannot provide a valid current payout quote."""


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PayoutQuoteError(f"Deriv proposal is missing a valid {field}.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PayoutQuoteError(f"Deriv proposal contains an invalid {field}.") from None
    if not result.is_finite() or result <= 0:
        raise PayoutQuoteError(f"Deriv proposal {field} must be finite and positive.")
    return result


def _normalize_proposal(
    payload: Any,
    *,
    side: str,
    symbol: str,
    currency: str,
    stake: Decimal,
    now_epoch: float,
    max_quote_age_seconds: int,
) -> dict:
    if not isinstance(payload, dict):
        raise PayoutQuoteError("Deriv proposal response is malformed.")
    ask_price = _decimal(payload.get("ask_price"), "ask_price")
    payout = _decimal(payload.get("payout"), "payout")
    if payout <= ask_price:
        raise PayoutQuoteError("Deriv proposal payout must exceed its ask price.")

    spot_time = payload.get("spot_time")
    if type(spot_time) is not int or spot_time <= 0:
        raise PayoutQuoteError("Deriv proposal has no valid spot_time.")
    quote_age = now_epoch - spot_time
    if quote_age < -2 or quote_age > max_quote_age_seconds:
        raise PayoutQuoteError("Deriv proposal spot quote is stale or future-dated.")

    proposal_id = payload.get("id")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        raise PayoutQuoteError("Deriv proposal has no valid proposal ID.")

    return {
        "side": side,
        "contract_type": CONTRACT_TYPES[side],
        "symbol": symbol,
        "currency": currency,
        "stake": str(stake),
        "ask_price": str(ask_price),
        "payout": str(payout),
        "profit_if_win": str(payout - ask_price),
        "loss_if_lose": str(ask_price),
        "proposal_id": proposal_id,
        "spot": payload.get("spot"),
        "spot_time": spot_time,
        "quote_age_seconds": max(0.0, quote_age),
        "date_start": payload.get("date_start"),
        "date_expiry": payload.get("date_expiry"),
        "longcode": payload.get("longcode"),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_even_odd_payout_quotes(
    *,
    symbol: str,
    currency: str,
    stake: Decimal | str | int | float,
    timeout_seconds: float = 10.0,
    max_quote_age_seconds: int = 10,
) -> dict:
    """Fetch current one-tick Even and Odd proposal terms without buying."""
    if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("Invalid Deriv symbol.")
    if not isinstance(currency, str) or not CURRENCY_PATTERN.fullmatch(currency):
        raise ValueError("Invalid currency code.")
    if isinstance(stake, bool):
        raise ValueError("Stake must be a finite positive amount.")
    try:
        stake_amount = Decimal(str(stake))
    except (InvalidOperation, ValueError):
        raise ValueError("Stake must be a finite positive amount.") from None
    if not stake_amount.is_finite() or stake_amount <= 0:
        raise ValueError("Stake must be a finite positive amount.")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive.")
    if type(max_quote_age_seconds) is not int or max_quote_age_seconds < 1:
        raise ValueError("max_quote_age_seconds must be a positive integer.")

    requests = {
        1: {
            "proposal": 1,
            "amount": float(stake_amount),
            "basis": "stake",
            "contract_type": "DIGITEVEN",
            "currency": currency,
            "duration": 1,
            "duration_unit": "t",
            "underlying_symbol": symbol,
            "req_id": 1,
        },
        2: {
            "proposal": 1,
            "amount": float(stake_amount),
            "basis": "stake",
            "contract_type": "DIGITODD",
            "currency": currency,
            "duration": 1,
            "duration_unit": "t",
            "underlying_symbol": symbol,
            "req_id": 2,
        },
    }
    received: dict[int, dict] = {}
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    now_epoch = datetime.now(timezone.utc).timestamp()

    try:
        async with websockets.connect(
            DERIV_PUBLIC_WS_URL,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
            close_timeout=5,
            max_size=1_000_000,
        ) as websocket:
            for request in requests.values():
                await websocket.send(json.dumps(request))

            while len(received) < 2:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise PayoutQuoteError("Timed out waiting for both Deriv payout proposals.")
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("error"):
                    error = message["error"]
                    detail = (
                        error.get("message", "Deriv rejected the proposal request.")
                        if isinstance(error, dict)
                        else "Deriv rejected the proposal request."
                    )
                    raise PayoutQuoteError(detail)
                if message.get("msg_type") != "proposal":
                    continue
                req_id = message.get("req_id")
                proposal = message.get("proposal")
                if req_id in requests and isinstance(proposal, dict):
                    received[req_id] = proposal
    except PayoutQuoteError:
        raise
    except Exception as exc:
        raise PayoutQuoteError("Unable to retrieve current Deriv payout proposals.") from exc

    quotes = {
        "even": _normalize_proposal(
            received[1],
            side="even",
            symbol=symbol,
            currency=currency,
            stake=stake_amount,
            now_epoch=now_epoch,
            max_quote_age_seconds=max_quote_age_seconds,
        ),
        "odd": _normalize_proposal(
            received[2],
            side="odd",
            symbol=symbol,
            currency=currency,
            stake=stake_amount,
            now_epoch=now_epoch,
            max_quote_age_seconds=max_quote_age_seconds,
        ),
    }
    return {
        "symbol": symbol,
        "currency": currency,
        "stake": str(stake_amount),
        "duration": 1,
        "duration_unit": "t",
        "quotes": quotes,
        "execution_enabled": False,
        "warning": "Read-only price proposals. No contract was purchased.",
    }
