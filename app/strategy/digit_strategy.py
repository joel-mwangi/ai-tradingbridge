"""Pure rule engine for the TradeBridge Digit Analytics strategy.

No network, database, UI, or trade-execution side effects belong in this module.
Percentage observations must come from a verified upstream Digit Analytics source.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

UNDER_DIGITS = frozenset(range(5))
OVER_DIGITS = frozenset(range(5, 10))
ALL_DIGITS = frozenset(range(10))


class Group(str, Enum):
    UNDER = "under"
    OVER = "over"


class Decision(str, Enum):
    OBSERVE = "observe"
    WAIT_BOTH_QUALIFY = "wait_both_qualify"
    SELECT_UNDER = "select_under"
    SELECT_OVER = "select_over"
    INVALID_DATA = "invalid_data"


@dataclass(frozen=True)
class TimestampedPercentageObservation:
    """One complete percentage snapshot from an upstream source.

    The timestamp must be the source event time, not a synthetic heartbeat.
    """
    observed_at: datetime
    percentages: Mapping[int, float]


def validate_initial_observation_window(
    observations: Sequence[TimestampedPercentageObservation],
    started_at: datetime,
    evaluated_at: datetime,
    window_seconds: float = 10.0,
) -> tuple[bool, str]:
    """Validate evidence for the initial 10-second window; never invent updates.

    This validates event timestamps and completeness, not source authenticity.
    The caller must only pass observations from a verified upstream source.
    At least two distinct snapshots are required so a single value cannot
    appear stable by having a zero spread.
    """
    if (
        not isinstance(started_at, datetime)
        or not isinstance(evaluated_at, datetime)
        or started_at.tzinfo is None
        or started_at.utcoffset() is None
        or evaluated_at.tzinfo is None
        or evaluated_at.utcoffset() is None
    ):
        return False, "Session start and evaluation timestamps must be timezone-aware."
    if not math.isfinite(window_seconds) or window_seconds <= 0:
        return False, "Window duration must be a positive finite number."

    start_utc = started_at.astimezone(timezone.utc)
    evaluated_utc = evaluated_at.astimezone(timezone.utc)
    boundary = start_utc + timedelta(seconds=window_seconds)
    if evaluated_utc < boundary:
        return False, "The initial observation window has not elapsed."
    if len(observations) < 2:
        return False, "At least two real percentage updates are required."

    previous_timestamp = None
    for index, observation in enumerate(observations):
        if not isinstance(observation, TimestampedPercentageObservation):
            return False, f"Observation {index} is not a timestamped percentage snapshot."
        timestamp = observation.observed_at
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return False, f"Observation {index} must have a timezone-aware source timestamp."
        timestamp = timestamp.astimezone(timezone.utc)
        if timestamp < start_utc or timestamp > boundary:
            return False, f"Observation {index} falls outside the initial window."
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            return False, "Observation timestamps must be strictly increasing."
        values = observation.percentages
        if not ALL_DIGITS.issubset(values):
            return False, f"Observation {index} must contain all ten digit percentages."
        if any(not _valid_percentage(values[d]) for d in ALL_DIGITS):
            return False, f"Observation {index} contains an invalid percentage."
        previous_timestamp = timestamp

    return True, "Initial observation window has valid timestamped percentage snapshots."


@dataclass(frozen=True)
class GroupEvaluation:
    group: Group
    threshold_passes: bool
    stability_passes: bool
    qualifies: bool
    reason: str


@dataclass(frozen=True)
class QualificationResult:
    decision: Decision
    under: GroupEvaluation
    over: GroupEvaluation
    trigger_digit: int | None = None
    reason: str = ""


def digits_for(group: Group) -> frozenset[int]:
    return UNDER_DIGITS if group is Group.UNDER else OVER_DIGITS


def _valid_percentage(value: object) -> bool:
    """Accept finite real percentage values only; bool is not a percentage."""
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and 0.0 <= number <= 100.0


def evaluate_thresholds(
    percentages: Mapping[int, float], group: Group
) -> tuple[bool, str]:
    """Pass if fewer than two digits are >=10% and none is >=10.5%."""
    digits = digits_for(group)
    if not digits.issubset(percentages):
        return False, "Missing one or more digit percentages."
    if any(not _valid_percentage(percentages[d]) for d in digits):
        return False, "Percentages must be finite numbers between 0 and 100."
    values = [float(percentages[d]) for d in digits]
    if any(value >= 10.5 for value in values):
        return False, "At least one digit is at or above 10.5%."
    if sum(value >= 10.0 for value in values) > 1:
        return False, "Two or more digits are at or above 10%."
    return True, "Percentage thresholds pass."


def evaluate_stability(
    observations: Sequence[Mapping[int, float]],
    group: Group,
    max_spread: float = 0.5,
) -> tuple[bool, str]:
    """Require each group's digit to have a max-minus-min spread <= 0.5 pp."""
    if not _valid_percentage(max_spread):
        return False, "Maximum spread must be a finite number between 0 and 100."
    if not observations:
        return False, "No percentage observations."
    digits = digits_for(group)
    for index, row in enumerate(observations):
        if not digits.issubset(row):
            return False, f"Observation {index} is missing group percentages."
        if any(not _valid_percentage(row[d]) for d in digits):
            return False, f"Observation {index} contains an invalid percentage."
    for digit in sorted(digits):
        values = [float(row[digit]) for row in observations]
        spread = max(values) - min(values)
        if spread > max_spread:
            return False, f"Digit {digit} spread {spread:.4f} exceeds {max_spread:.4f}."
    return True, f"All digit spreads are <= {max_spread:.1f} percentage points."


def evaluate_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
    group: Group,
) -> GroupEvaluation:
    threshold_ok, threshold_reason = evaluate_thresholds(percentages, group)
    stability_ok, stability_reason = evaluate_stability(observations, group)
    return GroupEvaluation(
        group, threshold_ok, stability_ok, threshold_ok and stability_ok,
        f"{threshold_reason} {stability_reason}",
    )


def choose_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
) -> QualificationResult:
    """Select only if exactly one group qualifies and its lowest digit is unique."""
    invalid_under = GroupEvaluation(Group.UNDER, False, False, False, "Incomplete or invalid data.")
    invalid_over = GroupEvaluation(Group.OVER, False, False, False, "Incomplete or invalid data.")
    if (
        not ALL_DIGITS.issubset(percentages)
        or any(not _valid_percentage(percentages[d]) for d in ALL_DIGITS)
    ):
        return QualificationResult(
            Decision.INVALID_DATA, invalid_under, invalid_over,
            reason="All ten current digit percentages must be finite values from 0 to 100.",
        )

    under = evaluate_group(percentages, observations, Group.UNDER)
    over = evaluate_group(percentages, observations, Group.OVER)
    if under.qualifies and over.qualifies:
        return QualificationResult(
            Decision.WAIT_BOTH_QUALIFY, under, over,
            reason="Both groups qualify; wait for another valid observation and reassess.",
        )
    if not under.qualifies and not over.qualifies:
        return QualificationResult(
            Decision.OBSERVE, under, over, reason="Neither group qualifies; continue observing.",
        )

    group = Group.UNDER if under.qualifies else Group.OVER
    group_digits = sorted(digits_for(group))
    lowest_value = min(float(percentages[d]) for d in group_digits)
    lowest_digits = [d for d in group_digits if float(percentages[d]) == lowest_value]
    # Deterministic tie-break: if multiple digits share the exact minimum,
    # choose the numerically smallest digit. This avoids arbitrary ordering and
    # keeps repeated evaluations reproducible.
    digit = min(lowest_digits)
    decision = Decision.SELECT_UNDER if group is Group.UNDER else Decision.SELECT_OVER
    return QualificationResult(
        decision, under, over, trigger_digit=digit,
        reason=f"{group.value.title()} alone qualifies; unique lowest-percentage digit selected.",
    )


@dataclass
class PatternTracker:
    """Detect TRIGGER -> selected-group -> selected-group -> opposite-group.

    Feed one actual last digit per ordered market tick. True is returned only
    for the tick that completes a pattern. This class never places a trade.
    """
    selected_group: Group
    trigger_digit: int
    progress: int = 0
    completed_patterns: int = 0

    def __post_init__(self) -> None:
        if type(self.trigger_digit) is not int or self.trigger_digit not in digits_for(self.selected_group):
            raise ValueError("Trigger digit must be an integer belonging to the selected group.")

    def reset(self) -> None:
        self.progress = 0

    def on_tick(self, digit: int) -> bool:
        if type(digit) is not int or digit not in ALL_DIGITS:
            raise ValueError("Last digit must be an integer from 0 to 9.")
        selected = digits_for(self.selected_group)
        opposite = OVER_DIGITS if self.selected_group is Group.UNDER else UNDER_DIGITS

        if self.progress == 0:
            if digit == self.trigger_digit:
                self.progress = 1
            return False

        if self.progress in (1, 2):
            if digit in selected:
                self.progress += 1
                return False
            self.progress = 1 if digit == self.trigger_digit else 0
            return False

        if digit in opposite:
            self.completed_patterns += 1
            self.progress = 0
            return True

        self.progress = 1 if digit == self.trigger_digit else 0
        return False

def choose_group_from_timestamped_observations(
    observations: Sequence[TimestampedPercentageObservation],
    started_at: datetime,
    evaluated_at: datetime,
    window_seconds: float = 10.0,
) -> QualificationResult:
    """Validate the initial window before evaluating thresholds and stability.

    This convenience entry point accepts only explicitly timestamped snapshots.
    It does not establish source authenticity; callers must provide observations
    from a verified source. No trade execution is performed.
    """
    valid, reason = validate_initial_observation_window(
        observations, started_at, evaluated_at, window_seconds
    )
    if not valid:
        invalid_under = GroupEvaluation(
            Group.UNDER, False, False, False, reason
        )
        invalid_over = GroupEvaluation(
            Group.OVER, False, False, False, reason
        )
        return QualificationResult(
            Decision.INVALID_DATA, invalid_under, invalid_over, reason=reason
        )

    snapshots = [observation.percentages for observation in observations]
    latest = snapshots[-1]
    return choose_group(latest, snapshots)
