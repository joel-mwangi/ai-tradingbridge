"""Fetch fresh Deriv digit-contract payouts without placing orders.

The public proposal endpoint is used only to price one-tick DIGITEVEN and
DIGITODD contracts. No proposal is bought or persisted as an executable order.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

import websockets


DERIV_PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
REQUEST_SIDES = {101: ("even", "DIGITEVEN"), 102: ("odd", "DIGITODD")}


class PayoutQuoteError(RuntimeError):
    """Raised when Deriv cannot provide a valid parity-contract quote."""


def _positive_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PayoutQuoteError(f"Deriv returned an invalid {field}.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PayoutQuoteError(f"Deriv returned an invalid {field}.") from None
    if not result.is_finite() or result <= 0:
        raise PayoutQuoteError(f"Deriv returned a non-positive or non-finite {field}.")
    return result


async def get_even_odd_payout_quote(
    *,
    symbol: str,
    currency: str,
    stake: Decimal | str | int | float,
    timeout_seconds: float = 10.0,
    max_quote_age_seconds: int = 10,
) -> dict:
    """Get matched one-tick Even and Odd quotes for a fixed session stake."""
    if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("Invalid Deriv market symbol.")
    if not isinstance(currency, str) or not CURRENCY_PATTERN.fullmatch(currency):
        raise ValueError("Currency must be a three-letter uppercase code.")
    if isinstance(stake, bool):
        raise ValueError("Stake must be a finite positive amount.")
    try:
        amount = Decimal(str(stake))
    except (InvalidOperation, ValueError):
        raise ValueError("Stake must be a finite positive amount.") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Stake must be a finite positive amount.")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be finite and positive.")
    if type(max_quote_age_seconds) is not int or max_quote_age_seconds < 1:
        raise ValueError("max_quote_age_seconds must be a positive integer.")

    requests = [
        {
            "proposal": 1,
            "amount": float(amount),
            "basis": "stake",
            "contract_type": contract_type,
            "currency": currency,
            "duration": 1,
            "duration_unit": "t",
            "underlying_symbol": symbol,
            "req_id": req_id,
        }
        for req_id, (_, contract_type) in REQUEST_SIDES.items()
    ]

    quotes: dict[str, dict] = {}
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        async with websockets.connect(
            DERIV_PUBLIC_WS_URL,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
            close_timeout=5,
            max_size=1_000_000,
        ) as websocket:
            for request in requests:
                await websocket.send(json.dumps(request))

            while len(quotes) < len(REQUEST_SIDES):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise PayoutQuoteError("Timed out waiting for both Deriv payout quotes.")
                raw_message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                try:
                    message = json.loads(raw_message)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("error"):
                    error = message["error"]
                    detail = (
                        error.get("message", "Deriv rejected a payout quote.")
                        if isinstance(error, dict)
                        else "Deriv rejected a payout quote."
                    )
                    raise PayoutQuoteError(str(detail)[:240])
                if message.get("msg_type") != "proposal":
                    continue

                req_id = message.get("req_id")
                if req_id not in REQUEST_SIDES:
                    continue
                side, contract_type = REQUEST_SIDES[req_id]
                if side in quotes:
                    continue
                proposal = message.get("proposal")
                echo = message.get("echo_req")
                if not isinstance(proposal, dict) or not isinstance(echo, dict):
                    raise PayoutQuoteError("Deriv returned an incomplete payout proposal.")
                if (
                    echo.get("underlying_symbol") != symbol
                    or echo.get("contract_type") != contract_type
                    or echo.get("currency") != currency
                    or echo.get("basis") != "stake"
                    or echo.get("duration") != 1
                    or echo.get("duration_unit") != "t"
                    or Decimal(str(echo.get("amount"))) != amount
                ):
                    raise PayoutQuoteError("Deriv proposal does not match the requested contract.")

                proposal_id = proposal.get("id")
                if not isinstance(proposal_id, str) or not proposal_id.strip():
                    raise PayoutQuoteError("Deriv proposal is missing its identifier.")
                ask_price = _positive_decimal(proposal.get("ask_price"), "ask_price")
                payout = _positive_decimal(proposal.get("payout"), "payout")
                profit_if_win = payout - ask_price
                if profit_if_win <= 0:
                    raise PayoutQuoteError("Deriv quote has no positive net profit on a win.")
                spot_time = proposal.get("spot_time")
                if type(spot_time) is not int or spot_time <= 0:
                    raise PayoutQuoteError("Deriv proposal has no valid spot_time.")
                quotes[side] = {
                    "contract_type": contract_type,
                    "proposal_id": proposal_id,
                    "ask_price": str(ask_price),
                    "payout": str(payout),
                    "profit_if_win": str(profit_if_win),
                    "loss_if_lose": str(ask_price),
                    "spot": proposal.get("spot"),
                    "spot_time": spot_time,
                }
        quoted_at = datetime.now(timezone.utc)
        final_epoch = quoted_at.timestamp()
        for quote in quotes.values():
            quote_age = final_epoch - quote["spot_time"]
            if quote_age < -2 or quote_age > max_quote_age_seconds:
                raise PayoutQuoteError("Deriv proposal spot quote is stale or future-dated.")
            quote["quote_age_seconds"] = max(0.0, quote_age)
        if abs(quotes["even"]["spot_time"] - quotes["odd"]["spot_time"]) > 2:
            raise PayoutQuoteError("Even and Odd payout quotes do not share a sufficiently close market snapshot.")
    except PayoutQuoteError:
        raise
    except asyncio.TimeoutError as exc:
        raise PayoutQuoteError("Timed out waiting for Deriv payout quotes.") from exc
    except Exception as exc:
        raise PayoutQuoteError("Unable to retrieve payout quotes from Deriv.") from exc

    return {
        "source": "deriv_public_websocket",
        "symbol": symbol,
        "currency": currency,
        "requested_stake": str(amount),
        "duration": 1,
        "duration_unit": "t",
        "quoted_at": quoted_at.isoformat(),
        "even": quotes["even"],
        "odd": quotes["odd"],
        "execution_authorized": False,
    }
