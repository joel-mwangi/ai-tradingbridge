"""Fail-closed validation for normalized percentage-feed snapshots.

This module does not connect to Deriv and does not prove source authenticity.
Only a separately reviewed, verified adapter should submit snapshots here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping


class FeedState(str, Enum):
    HEALTHY = "healthy"
    DISCONNECTED = "disconnected"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class PercentageSnapshot:
    source_id: str
    market_id: str
    observed_at: datetime
    received_at: datetime
    percentages: Mapping[int, float]
    event_id: str | None = None


@dataclass(frozen=True)
class FeedHealthResult:
    accepted: bool
    state: FeedState
    reason: str
    snapshot: PercentageSnapshot | None = None


class PercentageFeedHealth:
    """Validate snapshot structure, freshness, market, and event ordering.

    `max_age_seconds` must be explicitly configured from a verified feed's
    observed update cadence. This class deliberately has no default timeout.
    Call `mark_disconnected` when the transport reports a disconnect.
    After any unhealthy event, call `reset_after_reconnect` and rebuild a
    fresh observation window before strategy selection.
    """

    def __init__(
        self,
        expected_source_id: str,
        expected_market_id: str,
        max_age_seconds: float,
    ) -> None:
        if not expected_source_id.strip() or not expected_market_id.strip():
            raise ValueError("Expected source and market IDs must be non-empty.")
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be explicitly configured as a positive finite number.")
        self.expected_source_id = expected_source_id
        self.expected_market_id = expected_market_id
        self.max_age = timedelta(seconds=float(max_age_seconds))
        self.state = FeedState.HEALTHY
        self.last_observed_at: datetime | None = None
        self.last_received_at: datetime | None = None
        self._seen_event_ids: set[str] = set()

    def mark_disconnected(self, reason: str = "Feed transport disconnected.") -> FeedHealthResult:
        self.state = FeedState.DISCONNECTED
        return FeedHealthResult(False, self.state, reason)

    def reset_after_reconnect(self) -> None:
        """Clear continuity state; caller must collect a new full window."""
        self.state = FeedState.HEALTHY
        self.last_observed_at = None
        self.last_received_at = None
        self._seen_event_ids.clear()

    def _reject(self, reason: str) -> FeedHealthResult:
        self.state = FeedState.UNHEALTHY
        return FeedHealthResult(False, self.state, reason)

    def accept(
        self,
        snapshot: PercentageSnapshot,
        now: datetime,
    ) -> FeedHealthResult:
        """Accept only a complete, fresh, ordered snapshot for this market."""
        if self.state is FeedState.DISCONNECTED:
            return FeedHealthResult(
                False, self.state,
                "Feed is disconnected; reconnect and rebuild the observation window.",
            )
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return self._reject("Current time must be timezone-aware.")
        if not isinstance(snapshot, PercentageSnapshot):
            return self._reject("Snapshot has an unsupported type.")
        if snapshot.source_id != self.expected_source_id:
            return self._reject("Snapshot source does not match the verified source configuration.")
        if snapshot.market_id != self.expected_market_id:
            return self._reject("Snapshot market does not match the active session.")
        for label, value in (("observed_at", snapshot.observed_at), ("received_at", snapshot.received_at)):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                return self._reject(f"{label} must be timezone-aware.")

        now_utc = now.astimezone(timezone.utc)
        observed = snapshot.observed_at.astimezone(timezone.utc)
        received = snapshot.received_at.astimezone(timezone.utc)
        if observed > now_utc or received > now_utc:
            return self._reject("Snapshot timestamps cannot be in the future.")
        if received < observed:
            return self._reject("Receipt timestamp precedes the source event timestamp.")
        if now_utc - observed > self.max_age:
            return self._reject("Snapshot is stale based on the configured feed age limit.")
        if now_utc - received > self.max_age:
            return self._reject("Snapshot receipt is stale based on the configured feed age limit.")
        if self.last_observed_at is not None and observed <= self.last_observed_at:
            return self._reject("Source timestamps must be strictly increasing.")
        if self.last_received_at is not None and received < self.last_received_at:
            return self._reject("Receipt timestamps moved backwards.")
        if snapshot.event_id is not None:
            if not snapshot.event_id.strip():
                return self._reject("Event ID must be non-empty when provided.")
            if snapshot.event_id in self._seen_event_ids:
                return self._reject("Duplicate source event ID.")
        if not set(range(10)).issubset(snapshot.percentages):
            return self._reject("Snapshot must include percentages for all ten digits.")
        for digit in range(10):
            value = snapshot.percentages[digit]
            if isinstance(value, bool):
                return self._reject(f"Digit {digit} percentage cannot be boolean.")
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                return self._reject(f"Digit {digit} percentage must be numeric.")
            if not math.isfinite(numeric) or not 0 <= numeric <= 100:
                return self._reject(f"Digit {digit} percentage must be finite and in [0, 100].")

        self.last_observed_at = observed
        self.last_received_at = received
        if snapshot.event_id is not None:
            self._seen_event_ids.add(snapshot.event_id)
        self.state = FeedState.HEALTHY
        return FeedHealthResult(True, self.state, "Snapshot accepted.", snapshot)
