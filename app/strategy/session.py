"""Small, side-effect-free session state machine for the digit strategy.

This module does not connect to a transport or place trades. Callers must only
submit snapshots accepted by a configured PercentageFeedHealth monitor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.strategy.digit_strategy import (
    Decision,
    Group,
    PatternTracker,
    QualificationResult,
    TimestampedPercentageObservation,
    choose_group_from_timestamped_observations,
)
from app.strategy.feed_health import FeedHealthResult, PercentageFeedHealth, PercentageSnapshot


class SessionState(str, Enum):
    IDLE = "idle"
    OBSERVING = "observing"
    SELECTED = "selected"
    WAITING_FOR_MORE_DATA = "waiting_for_more_data"
    BLOCKED = "blocked"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SessionUpdate:
    state: SessionState
    reason: str
    qualification: QualificationResult | None = None
    pattern_completed: bool = False


@dataclass
class DigitStrategySession:
    """One fresh strategy session; no trade-execution responsibilities.

    Every call to start() clears observations, selection, pattern progress, and
    session-local counters. It does not reset the feed monitor: transport/feed
    continuity is managed separately. Snapshots must first be accepted by that
    monitor. An invalid feed event blocks this session until a new session is
    started after feed recovery.
    """

    feed_health: PercentageFeedHealth
    window_seconds: float = 10.0
    state: SessionState = SessionState.IDLE
    started_at: datetime | None = None
    observations: list[TimestampedPercentageObservation] = field(default_factory=list)
    selected_group: Group | None = None
    trigger_digit: int | None = None
    pattern_tracker: PatternTracker | None = None
    qualification: QualificationResult | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, (int, float))
            or self.window_seconds <= 0
        ):
            raise ValueError("window_seconds must be positive.")

    def start(self, started_at: datetime) -> SessionUpdate:
        if not isinstance(started_at, datetime) or started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("Session start must be timezone-aware.")
        self.started_at = started_at.astimezone(timezone.utc)
        self.observations.clear()
        self.selected_group = None
        self.trigger_digit = None
        self.pattern_tracker = None
        self.qualification = None
        self.state = SessionState.OBSERVING
        return SessionUpdate(self.state, "Fresh session started; initial observation window reset.")

    def add_snapshot(
        self, snapshot: PercentageSnapshot, now: datetime
    ) -> SessionUpdate:
        if self.state not in (SessionState.OBSERVING, SessionState.WAITING_FOR_MORE_DATA) or self.started_at is None:
            return SessionUpdate(self.state, "Session is not accepting observations.")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, "Current timestamp must be timezone-aware.")

        health: FeedHealthResult = self.feed_health.accept(snapshot, now)
        if not health.accepted or health.snapshot is None:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, f"Feed rejected snapshot: {health.reason}")

        observed_at = health.snapshot.observed_at.astimezone(timezone.utc)
        if observed_at < self.started_at:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, "Snapshot predates this strategy session.")

        self.observations.append(
            TimestampedPercentageObservation(observed_at, dict(health.snapshot.percentages))
        )
        if self.state is SessionState.WAITING_FOR_MORE_DATA:
            # Reassess on each new real percentage event, never on ticks.
            return self.evaluate(now)
        return SessionUpdate(self.state, "Fresh timestamped snapshot recorded.")

    def evaluate(self, now: datetime) -> SessionUpdate:
        if self.state not in (SessionState.OBSERVING, SessionState.WAITING_FOR_MORE_DATA) or self.started_at is None:
            return SessionUpdate(self.state, "Session is not ready for percentage-window evaluation.", self.qualification)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, "Evaluation timestamp must be timezone-aware.")
        now_utc = now.astimezone(timezone.utc)
        if now_utc < self.started_at + timedelta(seconds=self.window_seconds):
            return SessionUpdate(self.state, "Initial observation window has not elapsed.")

        if self.state is SessionState.OBSERVING:
            window_start = self.started_at
            window_end = self.started_at + timedelta(seconds=self.window_seconds)
            if now_utc < window_end:
                return SessionUpdate(self.state, "Initial observation window has not elapsed.")
            # Initial qualification uses only events from the first window.
            window_observations = [
                item for item in self.observations
                if window_start <= item.observed_at.astimezone(timezone.utc) <= window_end
            ]
        else:
            # Rolling reassessment excludes expired observations.
            window_start = now_utc - timedelta(seconds=self.window_seconds)
            window_observations = [
                item for item in self.observations
                if window_start <= item.observed_at.astimezone(timezone.utc) <= now_utc
            ]
            self.observations = window_observations

        if len(window_observations) < 2:
            if self.state is SessionState.OBSERVING:
                self.state = SessionState.BLOCKED
                return SessionUpdate(self.state, "Initial window needs at least two real percentage updates.")
            return SessionUpdate(
                self.state,
                "Waiting for at least two real percentage updates inside the rolling 10-second window.",
                self.qualification,
            )

        result = choose_group_from_timestamped_observations(
            window_observations, window_start, now_utc, self.window_seconds
        )
        self.qualification = result
        if result.decision is Decision.SELECT_UNDER:
            self.selected_group = Group.UNDER
        elif result.decision is Decision.SELECT_OVER:
            self.selected_group = Group.OVER
        elif result.decision is Decision.INVALID_DATA:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, result.reason, result)
        else:
            self.state = SessionState.WAITING_FOR_MORE_DATA
            return SessionUpdate(
                self.state,
                f"{result.reason} Reassessing on each new percentage snapshot using a rolling {self.window_seconds:g}-second window.",
                result,
            )

        self.trigger_digit = result.trigger_digit
        self.pattern_tracker = PatternTracker(self.selected_group, self.trigger_digit)
        self.state = SessionState.SELECTED
        return SessionUpdate(self.state, result.reason, result)

    def on_tick(self, digit: int) -> SessionUpdate:
        if self.state is not SessionState.SELECTED or self.pattern_tracker is None:
            return SessionUpdate(self.state, "Pattern tracking is unavailable until one group is selected.")
        try:
            completed = self.pattern_tracker.on_tick(digit)
        except ValueError as exc:
            self.state = SessionState.BLOCKED
            return SessionUpdate(self.state, str(exc))
        return SessionUpdate(
            self.state,
            "Pattern completed; no trade was submitted." if completed else "Pattern not yet complete.",
            self.qualification,
            pattern_completed=completed,
        )

    def stop(self) -> SessionUpdate:
        self.state = SessionState.STOPPED
        self.pattern_tracker = None
        return SessionUpdate(self.state, "Session stopped; no new strategy events will be processed.")
