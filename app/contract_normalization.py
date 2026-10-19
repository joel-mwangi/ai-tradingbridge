"""Canonical normalization for terminal Deriv contract responses.

The provider response is untrusted input. Preserve explicit zero values, prefer
provider-confirmed profit, and never infer a settlement from a non-terminal update.
"""
from __future__ import annotations

import math
from typing import Any


_TERMINAL_STATUSES = {"sold", "expired", "won", "lost", "cancelled"}


def parse_deriv_number(value: Any) -> float | None:
    """Return a finite number; preserve zero and reject booleans/invalid values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def is_deriv_true(value: Any) -> bool:
    """Parse provider boolean flags without treating the string 'false' as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def provider_status(contract: dict[str, Any]) -> str:
    value = contract.get("status")
    return value.strip().lower() if isinstance(value, str) else ""


def is_terminal_contract(contract: dict[str, Any]) -> bool:
    """Identify terminal contract updates using status and explicit terminal flags."""
    return (
        provider_status(contract) in _TERMINAL_STATUSES
        or is_deriv_true(contract.get("is_sold"))
        or is_deriv_true(contract.get("is_expired"))
    )


def normalize_settlement(
    contract: dict[str, Any], *, stake: float
) -> dict[str, float | str] | None:
    """Normalize one terminal Deriv contract to confirmed stake/payout/profit/outcome.

    Returns None when the update is not terminal or the monetary result cannot be
    established safely. A losing terminal contract with no explicit profit has zero
    payout; an explicit provider profit takes precedence over derived arithmetic.
    """
    if not is_terminal_contract(contract):
        return None

    status = provider_status(contract)
    explicit_profit = parse_deriv_number(contract.get("profit"))
    explicit_payout = parse_deriv_number(contract.get("payout"))
    sell_price = parse_deriv_number(contract.get("sell_price"))

    if status == "lost" and explicit_profit is None:
        payout = 0.0
        profit = -stake
    else:
        if status == "sold" or is_deriv_true(contract.get("is_sold")):
            payout = sell_price if sell_price is not None else explicit_payout
        else:
            payout = explicit_payout

        if explicit_profit is not None:
            profit = explicit_profit
            if payout is None:
                payout = stake + profit
        elif payout is not None:
            profit = payout - stake
        else:
            return None

    if payout is None or not math.isfinite(payout) or not math.isfinite(profit):
        return None

    outcome = "won" if profit > 0 else "lost" if profit < 0 else "breakeven"
    if status == "cancelled" and profit == 0:
        outcome = "cancelled"

    return {
        "provider_status": status or "unknown",
        "payout": round(payout, 8),
        "profit": round(profit, 8),
        "outcome": outcome,
    }
