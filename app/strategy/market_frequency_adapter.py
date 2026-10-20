"""Adapter from TradeBridge backend tick-frequency output to strategy snapshots.

The input must be the backend's own `digit_frequency` payload. This adapter
does not claim these values are official Deriv Digit Analytics percentages.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.strategy.feed_health import PercentageSnapshot

SOURCE_ID = "tradebridge_backend_tick_frequency"
STRATEGY_WINDOW = "1000"


def to_percentage_snapshot(
    payload: Mapping[str, Any],
    received_at: datetime | None = None,
    window: str = STRATEGY_WINDOW,
) -> PercentageSnapshot:
    """Convert one backend frequency event into a validated-shape strategy snapshot."""
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
    if type(sample_size) is not int or sample_size < 2:
        raise ValueError("At least two valid observed ticks are required for a strategy snapshot.")

    raw_percentages = summary.get("percentages")
    if not isinstance(raw_percentages, Mapping):
        raise ValueError("Frequency payload must include digit percentages.")
    percentages: dict[int, float] = {}
    for digit in range(10):
        value = raw_percentages.get(str(digit))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Digit {digit} percentage is unavailable.")
        percentages[digit] = float(value)

    calculated_at = payload.get("calculated_at")
    if isinstance(calculated_at, bool) or not isinstance(calculated_at, (int, float)):
        raise ValueError("Frequency payload must include a numeric calculation timestamp.")
    observed_at = datetime.fromtimestamp(float(calculated_at), tz=timezone.utc)
    received = received_at or datetime.now(timezone.utc)
    if received.tzinfo is None or received.utcoffset() is None:
        raise ValueError("Receipt timestamp must be timezone-aware.")

    latest_tick_epoch = payload.get("latest_tick_epoch")
    event_id = f"{symbol}:{latest_tick_epoch}:{calculated_at}"
    return PercentageSnapshot(
        source_id=SOURCE_ID,
        market_id=symbol,
        observed_at=observed_at,
        received_at=received.astimezone(timezone.utc),
        percentages=percentages,
        event_id=event_id,
    )
