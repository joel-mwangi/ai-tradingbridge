"""Tick normalization for the independent live-only Even/Odd collector.

The persistent worker owns the public WebSocket connection. This module does
not request or normalize historical tick snapshots.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


DERIV_PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"

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


def normalize_live_tick(
    payload: Any,
    expected_symbol: str,
    *,
    subscription_id: str | None = None,
) -> dict:
    """Normalize a live tick while keeping tick and subscription IDs distinct."""
    normalized = normalize_tick(payload, expected_symbol)
    normalized["tick_id"] = normalized.pop("stream_id", None)
    normalized["stream_id"] = str(subscription_id) if subscription_id is not None else None
    return normalized
