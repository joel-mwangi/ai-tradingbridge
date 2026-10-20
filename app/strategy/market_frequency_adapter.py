"""Adapter for TradeBridge's backend-calculated tick-frequency snapshots.

This is deliberately NOT an adapter for official Deriv Digit Analytics. It
normalizes the `digit_frequency` object emitted by /deriv/live/market into
the strategy's percentage-snapshot contract. The backend calculation time is
the observation timestamp; latest_tick_epoch is retained in the event ID so
each snapshot can be traced to the newest observed market tick.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from app.strategy.feed_health import PercentageSnapshot

SOURCE_ID = "tradebridge_backend_tick_frequency"
STRATEGY_WINDOW = "1000"


def _finite_number(value: Any) -> bool:
    """Accept finite numeric values, excluding bool (a Python int subclass)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def to_percentage_snapshot(
    payload: Mapping[str, Any],
    received_at: datetime | None = None,
    window: str = STRATEGY_WINDOW,
) -> PercentageSnapshot:
    """Normalize one backend frequency event or fail closed.

    This function validates payload shape and numeric integrity. It does not
    authenticate the upstream connection or claim statistical/predictive
    meaning; callers must also pass the result through PercentageFeedHealth.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("Frequency event must be a mapping.")
    if payload.get("source") != SOURCE_ID:
        raise ValueError("Unexpected frequency source; expected TradeBridge backend tick frequency.")

    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Frequency payload must include a market symbol.")

    windows = payload.get("windows")
    summary = windows.get(window) if isinstance(windows, Mapping) else None
    if not isinstance(summary, Mapping):
        raise ValueError(f"Frequency payload is missing window {window}.")

    sample_size = summary.get("sample_size")
    if type(sample_size) is not int or not 2 <= sample_size <= int(window):
        raise ValueError(f"Window {window} must contain between 2 and {window} valid observed ticks.")

    raw_percentages = summary.get("percentages")
    if not isinstance(raw_percentages, Mapping):
        raise ValueError("Frequency payload must include digit percentages.")

    percentages: dict[int, float] = {}
    for digit in range(10):
        value = raw_percentages.get(str(digit))
        if not _finite_number(value) or not 0 <= float(value) <= 100:
            raise ValueError(f"Digit {digit} percentage must be finite and in [0, 100].")
        percentages[digit] = float(value)

    calculated_at = payload.get("calculated_at")
    latest_tick_epoch = payload.get("latest_tick_epoch")
    if not _finite_number(calculated_at):
        raise ValueError("Frequency payload must include a finite calculation timestamp.")
    if not _finite_number(latest_tick_epoch):
        raise ValueError("Frequency payload must include a finite latest observed tick timestamp.")

    try:
        observed_at = datetime.fromtimestamp(float(calculated_at), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Frequency calculation timestamp is outside the supported range.") from exc

    received = received_at or datetime.now(timezone.utc)
    if not isinstance(received, datetime) or received.tzinfo is None or received.utcoffset() is None:
        raise ValueError("Receipt timestamp must be timezone-aware.")

    event_id = f"{symbol}:{float(latest_tick_epoch):.6f}:{float(calculated_at):.6f}:{window}"
    return PercentageSnapshot(
        source_id=SOURCE_ID,
        market_id=symbol,
        observed_at=observed_at,
        received_at=received.astimezone(timezone.utc),
        percentages=percentages,
        event_id=event_id,
    )
