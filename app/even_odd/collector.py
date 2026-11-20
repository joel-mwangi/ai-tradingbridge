"""Dedicated public Deriv tick collector for Even/Odd Lab.

Each collection creates and closes its own public market-data WebSocket. It
does not use the legacy collector, shared strategy feed, or trading session.
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


class TickCollectionError(RuntimeError):
    """Upstream collection failure, retaining any valid ticks already received."""

    def __init__(self, message: str, *, ticks: list[dict] | None = None):
        super().__init__(message)
        self.ticks = list(ticks or [])


def normalize_tick(payload: Any, expected_symbol: str) -> dict:
    """Normalize one Deriv tick without binary-float rounding of its last digit."""
    if not isinstance(payload, dict):
        raise ValueError("Tick payload must be an object.")
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or symbol != expected_symbol:
        raise ValueError("Tick symbol does not match the requested market.")

    quote_raw = payload.get("quote")
    epoch_raw = payload.get("epoch")
    precision_raw = payload.get("pip_size")
    if quote_raw is None or epoch_raw is None or precision_raw is None:
        raise ValueError("Tick is missing quote, epoch, or pip_size.")
    if isinstance(quote_raw, bool) or isinstance(epoch_raw, bool) or isinstance(precision_raw, bool):
        raise ValueError("Boolean values are not valid tick fields.")

    try:
        quote = Decimal(str(quote_raw))
        epoch_decimal = Decimal(str(epoch_raw))
        precision_decimal = Decimal(str(precision_raw))
    except (InvalidOperation, ValueError):
        raise ValueError("Tick contains an invalid numeric field.") from None

    if not quote.is_finite() or quote <= 0:
        raise ValueError("Tick quote must be finite and positive.")
    if not epoch_decimal.is_finite() or epoch_decimal != epoch_decimal.to_integral_value():
        raise ValueError("Tick epoch must be an integer.")
    if not precision_decimal.is_finite() or precision_decimal != precision_decimal.to_integral_value():
        raise ValueError("Tick pip_size must be an integer.")

    epoch = int(epoch_decimal)
    precision = int(precision_decimal)
    if epoch <= 0 or not 0 <= precision <= 10:
        raise ValueError("Tick epoch or precision is outside the supported range.")

    formatted_quote = format(quote, f".{precision}f")
    last_digit = int(formatted_quote.replace(".", "")[-1])
    stream_id = payload.get("id")
    if stream_id is not None and not isinstance(stream_id, (str, int)):
        raise ValueError("Deriv stream id must be a string or integer.")

    return {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "epoch": epoch,
        "symbol": expected_symbol,
        "quote": formatted_quote,
        "pip_size": precision,
        "last_digit": last_digit,
        "parity": "even" if last_digit % 2 == 0 else "odd",
        "stream_id": str(stream_id) if stream_id is not None else None,
    }


async def collect_ticks(
    symbol: str,
    tick_count: int,
    timeout_seconds: int = 60,
) -> list[dict]:
    """Fetch a finite historical tick window in one request (not a live stream)."""
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("Invalid Deriv symbol.")
    if not 10 <= tick_count <= 300:
        raise ValueError("tick_count must be between 10 and 300.")
    if not 5 <= timeout_seconds <= 80:
        raise ValueError("timeout_seconds must be between 5 and 80.")

    request_id = 4101
    request = {
        "ticks_history": symbol,
        "count": tick_count,
        "end": "latest",
        "style": "ticks",
        "req_id": request_id,
    }
    try:
        async with websockets.connect(
            DERIV_PUBLIC_WS_URL,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
            close_timeout=5,
            max_size=1_000_000,
        ) as websocket:
            await websocket.send(json.dumps(request))
            raw_message = await asyncio.wait_for(
                websocket.recv(), timeout=timeout_seconds
            )
            try:
                message = json.loads(raw_message)
            except (TypeError, json.JSONDecodeError) as exc:
                raise TickCollectionError("Deriv returned an invalid history response.") from exc

            if not isinstance(message, dict):
                raise TickCollectionError("Deriv returned an invalid history response.")
            if message.get("req_id") not in (None, request_id):
                raise TickCollectionError("Deriv response did not match the history request.")
            if message.get("error"):
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                detail = f"Deriv rejected the historical tick request{f' ({code})' if code else ''}."
                raise TickCollectionError(detail)
            if message.get("msg_type") != "history":
                raise TickCollectionError("Deriv did not return historical tick data.")

            history = message.get("history")
            prices = history.get("prices") if isinstance(history, dict) else None
            times = history.get("times") if isinstance(history, dict) else None
            pip_size = message.get("pip_size")
            if (
                not isinstance(prices, list)
                or not isinstance(times, list)
                or len(prices) != len(times)
                or not prices
                or pip_size is None
            ):
                raise TickCollectionError("Deriv historical response is missing aligned prices, times, or pip_size.")

            collected: list[dict] = []
            for price, epoch in zip(prices, times):
                try:
                    collected.append(normalize_tick(
                        {
                            "symbol": symbol,
                            "quote": price,
                            "epoch": epoch,
                            "pip_size": pip_size,
                        },
                        symbol,
                    ))
                except ValueError:
                    continue

            if len(collected) < 10:
                raise TickCollectionError(
                    f"Insufficient valid historical ticks received: {len(collected)} of {tick_count} requested.",
                    ticks=collected,
                )
            if len(collected) < tick_count:
                raise TickCollectionError(
                    f"Deriv returned {len(collected)} of {tick_count} requested historical ticks.",
                    ticks=collected,
                )
            if len(collected) > tick_count:
                collected = collected[-tick_count:]
            return collected
    except TickCollectionError:
        raise
    except asyncio.TimeoutError as exc:
        raise TickCollectionError(
            "Deriv historical tick request timed out before the history response arrived."
        ) from exc
    except Exception as exc:
        raise TickCollectionError(
            "Unable to retrieve historical ticks from the Deriv public API."
        ) from exc
