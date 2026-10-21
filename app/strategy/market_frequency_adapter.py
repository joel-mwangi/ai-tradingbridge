"""Adapter for TradeBridge's backend-calculated tick-frequency snapshots.

This is deliberately NOT an adapter for official Deriv Digit Analytics. It
normalizes the `digit_frequency` object emitted by /deriv/live/market into
the strategy's percentage-snapshot contract. The provider's latest tick epoch
is the observation timestamp; calculation time is diagnostic metadata.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from app.strategy.feed_health import PercentageSnapshot

SOURCE_ID = "tradebridge_backend_tick_frequency"
DEFAULT_STRATEGY_WINDOW = "1000"
SUPPORTED_WINDOWS = ("50", "100", "250", "500", "1000")
STRATEGY_WINDOW = DEFAULT_STRATEGY_WINDOW


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

    Check the count/percentage contract as well as field types: valid-looking
    percentages must not be allowed to disagree with the actual histogram.
    This does not authenticate the upstream connection or imply prediction.
    """
    if window not in SUPPORTED_WINDOWS:
        raise ValueError(f"Unsupported frequency window {window}; expected one of {', '.join(SUPPORTED_WINDOWS)}.")
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
    if summary.get("window_size") != int(window):
        raise ValueError(f"Window {window} has an inconsistent window_size.")

    raw_counts = summary.get("counts")
    if not isinstance(raw_counts, Mapping):
        raise ValueError("Frequency payload must include digit counts.")
    counts: dict[int, int] = {}
    for digit in range(10):
        value = raw_counts.get(str(digit))
        if type(value) is not int or value < 0:
            raise ValueError(f"Digit {digit} count must be a non-negative integer.")
        counts[digit] = value
    if sum(counts.values()) != sample_size:
        raise ValueError("Digit counts must sum exactly to sample_size.")

    raw_percentages = summary.get("percentages")
    if not isinstance(raw_percentages, Mapping):
        raise ValueError("Frequency payload must include digit percentages.")

    percentages: dict[int, float] = {}
    for digit in range(10):
        value = raw_percentages.get(str(digit))
        if not _finite_number(value) or not 0 <= float(value) <= 100:
            raise ValueError(f"Digit {digit} percentage must be finite and in [0, 100].")
        numeric = float(value)
        expected = round(counts[digit] / sample_size * 100, 1)
        if numeric != expected:
            raise ValueError(
                f"Digit {digit} percentage does not match its count and sample_size."
            )
        percentages[digit] = numeric

    calculated_at = payload.get("calculated_at")
    latest_tick_epoch = payload.get("latest_tick_epoch")
    latest_tick_event_id = payload.get("latest_tick_event_id")
    if not _finite_number(calculated_at):
        raise ValueError("Frequency payload must include a finite calculation timestamp.")
    if not _finite_number(latest_tick_epoch):
        raise ValueError("Frequency payload must include a finite latest observed tick timestamp.")
    if not isinstance(latest_tick_event_id, str) or not latest_tick_event_id.strip():
        raise ValueError("Frequency payload must include a stable latest source tick event ID.")

    try:
        observed_at = datetime.fromtimestamp(float(latest_tick_epoch), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Latest source tick timestamp is outside the supported range.") from exc

    received = received_at or datetime.now(timezone.utc)
    if not isinstance(received, datetime) or received.tzinfo is None or received.utcoffset() is None:
        raise ValueError("Receipt timestamp must be timezone-aware.")

    event_id = f"{symbol}:{latest_tick_event_id}:{window}"
    return PercentageSnapshot(
        source_id=SOURCE_ID,
        market_id=symbol,
        observed_at=observed_at,
        received_at=received.astimezone(timezone.utc),
        percentages=percentages,
        event_id=event_id,
    )
