"""Pure rule engine for TradeBridge's explicit Over 3 / Under 6 profiles.

This module ranks all ten observed digit frequencies and evaluates the two
profiles in the approved strategy specification. It has no transport, database,
UI, or trade-execution side effects. Percentages are TradeBridge-calculated
observed-tick frequencies unless a separately verified source is configured.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Sequence

ALL_DIGITS = frozenset(range(10))

# The two strategy profiles are intentionally explicit. Do not infer a new
# barrier profile from these sets: Over 6, Under 3, etc. need their own rules.
OVER_3_FILTER_DIGITS = frozenset({0, 1, 2, 3})
UNDER_6_FILTER_DIGITS = frozenset({6, 7, 8, 9})

OVER_3_RED_YELLOW_ZONE = frozenset({0, 1, 2, 3, 4})
OVER_3_GREEN_BLUE_ZONE = frozenset({5, 6, 7, 8, 9})
UNDER_6_RED_YELLOW_ZONE = frozenset({5, 6, 7, 8, 9})
UNDER_6_GREEN_BLUE_ZONE = frozenset({0, 1, 2, 3, 4})

OVER_3_ACTIVITY_DIGITS = OVER_3_FILTER_DIGITS
OVER_3_CONFIRM_DIGITS = frozenset({6, 7, 8, 9})
UNDER_6_ACTIVITY_DIGITS = UNDER_6_FILTER_DIGITS
UNDER_6_CONFIRM_DIGITS = frozenset({0, 1, 2, 3})

STABILITY_SECONDS = 15.0
PERCENTAGE_TRIGGER_RISE = 0.1
OVER_3_RED_YELLOW_MAX_PERCENT = 9.0
FILTER_LIMIT = 9.7
FILTER_EXCEPTION_MAX = 10.5
MIN_ACTIVITY_TICKS = 2


class Group(str, Enum):
    UNDER = "under"  # Explicitly means the documented Under 6 profile.
    OVER = "over"    # Explicitly means the documented Over 3 profile.


class Decision(str, Enum):
    OBSERVE = "observe"
    WAIT_BOTH_QUALIFY = "wait_both_qualify"
    SELECT_UNDER = "select_under"
    SELECT_OVER = "select_over"
    INVALID_DATA = "invalid_data"


@dataclass(frozen=True)
class TimestampedPercentageObservation:
    """One complete percentage snapshot with its source event timestamp."""
    observed_at: datetime
    percentages: Mapping[int, float]


@dataclass(frozen=True)
class BarColorRanks:
    """All tied digits are retained in the corresponding rank set."""
    red: frozenset[int]
    yellow: frozenset[int]
    blue: frozenset[int]
    green: frozenset[int]

    @property
    def stability_signature(self) -> tuple[frozenset[int], frozenset[int]]:
        return self.red, self.green


@dataclass(frozen=True)
class GroupEvaluation:
    group: Group
    threshold_passes: bool
    stability_passes: bool
    color_positions_pass: bool
    trigger_passes: bool
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
    """Return the percentage-filter/trigger digits for the documented profile."""
    return UNDER_6_FILTER_DIGITS if group is Group.UNDER else OVER_3_FILTER_DIGITS


def activity_digits_for(group: Group) -> frozenset[int]:
    return UNDER_6_ACTIVITY_DIGITS if group is Group.UNDER else OVER_3_ACTIVITY_DIGITS


def confirmation_digits_for(group: Group) -> frozenset[int]:
    return UNDER_6_CONFIRM_DIGITS if group is Group.UNDER else OVER_3_CONFIRM_DIGITS


def _valid_percentage(value: object) -> bool:
    """Accept finite real percentage values only; bool is not a percentage."""
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and 0.0 <= number <= 100.0


def _valid_percentage_map(percentages: Mapping[int, float]) -> bool:
    return (
        isinstance(percentages, Mapping)
        and ALL_DIGITS.issubset(percentages)
        and all(_valid_percentage(percentages[d]) for d in ALL_DIGITS)
    )


def rank_bar_colors(percentages: Mapping[int, float]) -> BarColorRanks:
    """Rank percentages across digits 0–9, preserving every frequency tie.

    Rank order is based on distinct percentage levels (dense ranks): lowest is
    Red, next distinct level Yellow, highest Green, and previous distinct level
    Blue. A tied level includes every digit at that percentage.
    """
    if not _valid_percentage_map(percentages):
        raise ValueError("All ten digit percentages must be finite values from 0 to 100.")

    numeric = {digit: float(percentages[digit]) for digit in ALL_DIGITS}
    levels = sorted(set(numeric.values()))
    low = levels[0]
    high = levels[-1]
    second_low = levels[1] if len(levels) >= 2 else None
    second_high = levels[-2] if len(levels) >= 2 else None

    return BarColorRanks(
        red=frozenset(d for d, value in numeric.items() if value == low),
        yellow=frozenset(d for d, value in numeric.items() if second_low is not None and value == second_low),
        blue=frozenset(d for d, value in numeric.items() if second_high is not None and value == second_high),
        green=frozenset(d for d, value in numeric.items() if value == high),
    )


def evaluate_thresholds(
    percentages: Mapping[int, float], group: Group
) -> tuple[bool, str]:
    """Apply the profile's four-digit <9.7% / single <=10.5% exception."""
    digits = digits_for(group)
    if not digits.issubset(percentages):
        return False, "Missing one or more profile-filter percentages."
    if any(not _valid_percentage(percentages[d]) for d in digits):
        return False, "Profile percentages must be finite numbers between 0 and 100."

    values = [float(percentages[d]) for d in sorted(digits)]
    below_limit = sum(value < FILTER_LIMIT for value in values)
    if below_limit == 4:
        return True, "All four profile digits are strictly below 9.7%."
    if below_limit == 3:
        exception_values = [value for value in values if value >= FILTER_LIMIT]
        if len(exception_values) == 1 and exception_values[0] <= FILTER_EXCEPTION_MAX:
            return True, "Three digits are below 9.7%; the remaining digit is <= 10.5%."
    return False, "Percentage filter failed: require four digits <9.7%, or three <9.7% with the remaining digit <=10.5%."


def evaluate_color_positions(
    percentages: Mapping[int, float], group: Group
) -> tuple[bool, str]:
    """Enforce the documented colour zones and Over 3's extra <9% filter."""
    try:
        ranks = rank_bar_colors(percentages)
    except ValueError as exc:
        return False, str(exc)

    if len(ranks.red) != 1:
        return False, "Entry blocked: two or more digits are tied Red; exactly one Red digit is required."
    if not ranks.yellow or not ranks.blue:
        return False, "Colour positions cannot be qualified without distinct second-lowest and second-highest ranks."

    if group is Group.OVER:
        red_yellow_zone = OVER_3_RED_YELLOW_ZONE
        green_blue_zone = OVER_3_GREEN_BLUE_ZONE
        if not ranks.red.issubset(red_yellow_zone) or not ranks.yellow.issubset(red_yellow_zone):
            return False, "Over 3 requires Red and Yellow among digits 0–4."
        if not ranks.green.issubset(green_blue_zone) or not ranks.blue.issubset(green_blue_zone):
            return False, "Over 3 requires Green and Blue among digits 5–9."
        if any(float(percentages[d]) >= OVER_3_RED_YELLOW_MAX_PERCENT for d in (ranks.red | ranks.yellow)):
            return False, "Over 3 requires both Red and Yellow percentages to be strictly below 9%."
        return True, "Over 3 colour zones pass; Red and Yellow are <9%."

    red_yellow_zone = UNDER_6_RED_YELLOW_ZONE
    green_blue_zone = UNDER_6_GREEN_BLUE_ZONE
    if not ranks.red.issubset(red_yellow_zone) or not ranks.yellow.issubset(red_yellow_zone):
        return False, "Under 6 requires Red and Yellow among digits 5–9."
    if not ranks.green.issubset(green_blue_zone) or not ranks.blue.issubset(green_blue_zone):
        return False, "Under 6 requires Green and Blue among digits 0–4."
    return True, "Under 6 colour zones pass; no additional Red/Yellow <9% filter applies."


def evaluate_stability(
    observations: Sequence[Mapping[int, float]],
    group: Group | None = None,
    max_spread: float | None = None,
) -> tuple[bool, str]:
    """Check unchanged Red/Green identity sets across snapshots.

    The legacy group and max_spread arguments are accepted for compatibility,
    but stability is identity-based, not a percentage-spread calculation.
    Elapsed time is enforced by the timestamped validator/session state machine.
    """
    del group, max_spread
    if len(observations) < 2:
        return False, "At least two real percentage snapshots are required to check Red/Green stability."

    signatures: list[tuple[frozenset[int], frozenset[int]]] = []
    for index, percentages in enumerate(observations):
        try:
            ranks = rank_bar_colors(percentages)
        except ValueError as exc:
            return False, f"Observation {index} is invalid: {exc}"
        if len(ranks.red) != 1:
            return False, f"Observation {index} has multiple Red digits; waiting for exactly one Red digit."
        signatures.append(ranks.stability_signature)

    if any(signature != signatures[0] for signature in signatures[1:]):
        return False, "Red or Green digit identities changed; restart the 15-second stability timer."
    return True, "Red and Green digit identities remained unchanged across the supplied snapshots."


def _trigger_digit(percentages: Mapping[int, float], group: Group) -> int | None:
    digits = digits_for(group)
    values = {digit: float(percentages[digit]) for digit in digits}
    low = min(values.values())
    lowest = [digit for digit, value in values.items() if value == low]
    return lowest[0] if len(lowest) == 1 else None


def evaluate_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
    group: Group,
) -> GroupEvaluation:
    threshold_ok, threshold_reason = evaluate_thresholds(percentages, group)
    stability_ok, stability_reason = evaluate_stability(observations)
    color_ok, color_reason = evaluate_color_positions(percentages, group)
    trigger_digit = _trigger_digit(percentages, group) if _valid_percentage_map(percentages) else None
    trigger_ok = trigger_digit is not None
    trigger_reason = (
        f"Unique trigger digit {trigger_digit} selected from profile digits."
        if trigger_ok
        else "Trigger blocked: the lowest percentage within the profile digits is tied."
    )
    qualifies = threshold_ok and stability_ok and color_ok and trigger_ok
    reasons = [threshold_reason, stability_reason, color_reason, trigger_reason]
    return GroupEvaluation(group, threshold_ok, stability_ok, color_ok, trigger_ok, qualifies, " ".join(reasons))


def choose_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
) -> QualificationResult:
    """Automatically select the sole qualifying explicit profile; never break ties."""
    invalid_under = GroupEvaluation(Group.UNDER, False, False, False, False, False, "Incomplete or invalid data.")
    invalid_over = GroupEvaluation(Group.OVER, False, False, False, False, False, "Incomplete or invalid data.")
    if not _valid_percentage_map(percentages):
        return QualificationResult(
            Decision.INVALID_DATA, invalid_under, invalid_over,
            reason="All ten current digit percentages must be finite values from 0 to 100.",
        )

    under = evaluate_group(percentages, observations, Group.UNDER)
    over = evaluate_group(percentages, observations, Group.OVER)
    if under.qualifies and over.qualifies:
        return QualificationResult(
            Decision.WAIT_BOTH_QUALIFY, under, over,
            reason="Both explicit profiles qualify; do not choose a direction arbitrarily.",
        )
    if not under.qualifies and not over.qualifies:
        return QualificationResult(
            Decision.OBSERVE, under, over,
            reason="Neither explicit profile qualifies; continue observing.",
        )

    group = Group.UNDER if under.qualifies else Group.OVER
    digit = _trigger_digit(percentages, group)
    if digit is None:
        # Defensive guard: evaluate_group should already have failed this case.
        return QualificationResult(
            Decision.OBSERVE, under, over,
            reason="The profile's lowest-frequency trigger digit is tied; continue observing.",
        )
    decision = Decision.SELECT_UNDER if group is Group.UNDER else Decision.SELECT_OVER
    return QualificationResult(
        decision, under, over, trigger_digit=digit,
        reason=f"{group.value.title()} profile alone qualifies; automatic direction selected.",
    )


def validate_initial_observation_window(
    observations: Sequence[TimestampedPercentageObservation],
    started_at: datetime,
    evaluated_at: datetime,
    window_seconds: float = STABILITY_SECONDS,
) -> tuple[bool, str]:
    """Validate real snapshots spanning a continuous 15-second ranking window."""
    if (
        not isinstance(started_at, datetime)
        or not isinstance(evaluated_at, datetime)
        or started_at.tzinfo is None
        or started_at.utcoffset() is None
        or evaluated_at.tzinfo is None
        or evaluated_at.utcoffset() is None
    ):
        return False, "Session start and evaluation timestamps must be timezone-aware."
    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, (int, float))
        or not math.isfinite(float(window_seconds))
        or window_seconds <= 0
    ):
        return False, "Stability duration must be a positive finite number."

    start_utc = started_at.astimezone(timezone.utc)
    evaluated_utc = evaluated_at.astimezone(timezone.utc)
    if evaluated_utc - start_utc < timedelta(seconds=float(window_seconds)):
        return False, f"The {window_seconds:g}-second stability period has not elapsed."
    if len(observations) < 2:
        return False, "At least two real percentage updates are required."

    previous_timestamp: datetime | None = None
    for index, observation in enumerate(observations):
        if not isinstance(observation, TimestampedPercentageObservation):
            return False, f"Observation {index} is not a timestamped percentage snapshot."
        timestamp = observation.observed_at
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return False, f"Observation {index} must have a timezone-aware source timestamp."
        timestamp = timestamp.astimezone(timezone.utc)
        if timestamp < start_utc or timestamp > evaluated_utc:
            return False, f"Observation {index} falls outside the stability period."
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            return False, "Observation timestamps must be strictly increasing."
        if not _valid_percentage_map(observation.percentages):
            return False, f"Observation {index} must contain all ten valid digit percentages."
        previous_timestamp = timestamp

    first_observed = observations[0].observed_at.astimezone(timezone.utc)
    last_observed = observations[-1].observed_at.astimezone(timezone.utc)
    if last_observed - first_observed < timedelta(seconds=float(window_seconds)):
        return False, f"Real snapshots must span the full {window_seconds:g}-second stability period."
    return True, "Timestamped observations span the full stability period."


def choose_group_from_timestamped_observations(
    observations: Sequence[TimestampedPercentageObservation],
    started_at: datetime,
    evaluated_at: datetime,
    window_seconds: float = STABILITY_SECONDS,
) -> QualificationResult:
    """Validate elapsed time, source timestamps, rankings, filters, and direction."""
    valid, reason = validate_initial_observation_window(
        observations, started_at, evaluated_at, window_seconds
    )
    invalid_under = GroupEvaluation(Group.UNDER, False, False, False, False, False, reason)
    invalid_over = GroupEvaluation(Group.OVER, False, False, False, False, False, reason)
    if not valid:
        return QualificationResult(Decision.INVALID_DATA, invalid_under, invalid_over, reason=reason)

    snapshots = [observation.percentages for observation in observations]
    return choose_group(snapshots[-1], snapshots)


@dataclass
class PatternTracker:
    """After a percentage trigger, require 2+ activity digits then a confirmation digit.

    The trigger is a percentage increase, not the trigger digit appearing as a
    tick. The caller arms this tracker only after the trigger percentage rises.
    """
    selected_group: Group
    trigger_digit: int
    required_activity_ticks: int = MIN_ACTIVITY_TICKS
    progress: int = 0
    completed_patterns: int = 0
    armed: bool = False

    def __post_init__(self) -> None:
        if type(self.trigger_digit) is not int or self.trigger_digit not in digits_for(self.selected_group):
            raise ValueError("Trigger digit must be an integer belonging to the selected profile's four digits.")
        if type(self.required_activity_ticks) is not int or self.required_activity_ticks < 2:
            raise ValueError("At least two activity ticks are required.")
    
    def arm(self) -> None:
        self.progress = 0
        self.armed = True

    def reset(self) -> None:
        self.progress = 0

    def on_tick(self, digit: int) -> bool:
        if type(digit) is not int or digit not in ALL_DIGITS:
            raise ValueError("Last digit must be an integer from 0 to 9.")
        if not self.armed:
            return False

        activity = activity_digits_for(self.selected_group)
        confirmation = confirmation_digits_for(self.selected_group)
        if digit in activity:
            self.progress += 1
            return False
        if digit in confirmation:
            if self.progress >= self.required_activity_ticks:
                self.completed_patterns += 1
                self.armed = False
                self.progress = 0
                return True
            self.progress = 0
            return False

        # Middle digits (4 and 5) break the consecutive activity sequence.
        self.progress = 0
        return False
