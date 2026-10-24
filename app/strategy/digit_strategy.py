"""Pure rule engine for barrier-aware TradeBridge digit strategies.

The selected barrier configures both candidate directions: Over evaluates digits
0..barrier and Under evaluates digits barrier..9. Red/Yellow and Green/Blue
zones remain the documented directional halves (0–4 / 5–9). No transport,
database, UI, or order-execution side effects belong in this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Sequence

ALL_DIGITS = frozenset(range(10))
LOW_COLOR_ZONE = frozenset(range(5))
HIGH_COLOR_ZONE = frozenset(range(5, 10))

STABILITY_SECONDS = 15.0
PERCENTAGE_TRIGGER_RISE = 0.1
OVER_RED_YELLOW_MAX_PERCENT = 9.0
FILTER_LIMIT = 9.7
FILTER_EXCEPTION_MAX = 10.5
MIN_ACTIVITY_TICKS = 1
DEFAULT_BARRIER = 5


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


def validate_barrier(barrier: object) -> bool:
    """Accept a digit barrier 0–9; each direction applies its own valid range."""
    return type(barrier) is int and barrier in ALL_DIGITS


def barrier_valid_for(group: Group, barrier: int) -> bool:
    """Deriv digit contracts cannot win at Over 9 or Under 0."""
    if not validate_barrier(barrier):
        return False
    if group is Group.OVER:
        return barrier <= 8
    return barrier >= 1


def strategy_profile_supported(group: Group, barrier: int) -> bool:
    """Return whether the generalized rules have non-empty activity and confirmation bands.

    Over uses activity digits 0..B and confirmation digits B+3..9.
    Under uses activity digits B..9 and confirmation digits 0..B-3.
    Empty confirmation bands cannot produce a signal and must fail closed.
    """
    if not barrier_valid_for(group, barrier):
        return False
    return bool(activity_digits_for(group, barrier) and confirmation_digits_for(group, barrier))


def digits_for(group: Group, barrier: int = DEFAULT_BARRIER) -> frozenset[int]:
    """Digits evaluated by the selected barrier: Over <= B, Under >= B."""
    if not validate_barrier(barrier):
        raise ValueError("Barrier must be an integer digit from 0 to 9.")
    if group is Group.OVER:
        return frozenset(range(0, barrier + 1))
    return frozenset(range(barrier, 10))


def activity_digits_for(group: Group, barrier: int = DEFAULT_BARRIER) -> frozenset[int]:
    """The selected direction's barrier-dependent activity class."""
    return digits_for(group, barrier)


def confirmation_digits_for(group: Group, barrier: int = DEFAULT_BARRIER) -> frozenset[int]:
    """Opposite-side confirmation band with the two-digit buffer from the examples.

    Over 3 confirms on 6–9 (B+3..9); Under 6 confirms on 0–3 (0..B-3).
    """
    if not validate_barrier(barrier):
        raise ValueError("Barrier must be an integer digit from 0 to 9.")
    if group is Group.OVER:
        return frozenset(range(barrier + 3, 10))
    return frozenset(range(0, max(barrier - 2, 0)))


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
    """Rank percentages across digits 0–9, preserving ties in each rank.

    Dense ranks use distinct percentage levels: lowest is Red, next distinct
    level Yellow, highest Green, and the preceding distinct level Blue.
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
    percentages: Mapping[int, float],
    group: Group,
    barrier: int = DEFAULT_BARRIER,
) -> tuple[bool, str]:
    """Allow at most one selected digit >=9.7%, strictly below 10.5%."""
    if not validate_barrier(barrier):
        return False, "Barrier must be an integer digit from 0 to 9."
    if not barrier_valid_for(group, barrier):
        return False, f"{group.value.title()} {barrier} is not a valid winning barrier."
    if not strategy_profile_supported(group, barrier):
        return False, f"{group.value.title()} {barrier} has no complete approved strategy profile; observation only."
    digits = digits_for(group, barrier)
    if not digits.issubset(percentages):
        return False, "Missing one or more barrier-filter percentages."
    if any(not _valid_percentage(percentages[d]) for d in digits):
        return False, "Barrier-filter percentages must be finite numbers between 0 and 100."

    below_limit = [d for d in digits if float(percentages[d]) < FILTER_LIMIT]
    required_below = len(digits) - 1
    if len(below_limit) < required_below:
        return False, f"Percentage filter failed: at least {required_below} of {len(digits)} selected digits must be strictly below 9.7%."
    exceptions = [d for d in digits if float(percentages[d]) >= FILTER_LIMIT]
    if exceptions:
        if len(exceptions) != 1 or not float(percentages[exceptions[0]]) < FILTER_EXCEPTION_MAX:
            return False, f"{group.value.title()} percentage filter failed: the one remaining digit must be strictly below 10.5%."
    return True, f"{group.value.title()} {barrier} percentage filter passes: at least {required_below} selected digits are below 9.7%."


def evaluate_color_positions(
    percentages: Mapping[int, float],
    group: Group,
    barrier: int = DEFAULT_BARRIER,
) -> tuple[bool, str]:
    """Place all four colour ranks on the correct side of the selected barrier."""
    if not barrier_valid_for(group, barrier):
        return False, f"{group.value.title()} {barrier} is not a valid winning barrier."
    if not strategy_profile_supported(group, barrier):
        return False, f"{group.value.title()} {barrier} has no complete approved strategy profile; observation only."
    try:
        ranks = rank_bar_colors(percentages)
    except ValueError as exc:
        return False, str(exc)

    if len(ranks.red) != 1:
        return False, "Entry blocked: two or more digits are tied Red; exactly one Red digit is required."
    if not ranks.yellow or not ranks.blue:
        return False, "Colour positions cannot be qualified without distinct second-lowest and second-highest ranks."

    if group is Group.OVER:
        # Over B's documented Over 3 example uses 0..B+1 for Red/Yellow
        # and B+2..9 for Green/Blue.
        boundary = min(barrier + 2, 10)
        red_yellow_zone = frozenset(range(0, boundary))
        green_blue_zone = frozenset(range(boundary, 10))
        if not ranks.red.issubset(red_yellow_zone) or not ranks.yellow.issubset(red_yellow_zone):
            return False, f"Over {barrier} requires Red and Yellow among digits 0–{min(barrier + 1, 9)}."
        if not ranks.green.issubset(green_blue_zone) or not ranks.blue.issubset(green_blue_zone):
            return False, f"Over {barrier} requires Green and Blue among digits {boundary}–9."
        if any(float(percentages[d]) >= OVER_RED_YELLOW_MAX_PERCENT for d in (ranks.red | ranks.yellow)):
            return False, f"Over {barrier} requires both Red and Yellow percentages to be strictly below 9%."
        return True, f"Over {barrier} colour positions pass; Red and Yellow are <9%."

    # Under B's documented Under 6 example uses B-1..9 for Red/Yellow
    # and 0..B-2 for Green/Blue.
    boundary = max(barrier - 1, 0)
    green_blue_zone = frozenset(range(0, boundary))
    red_yellow_zone = frozenset(range(boundary, 10))
    if not ranks.red.issubset(red_yellow_zone) or not ranks.yellow.issubset(red_yellow_zone):
        return False, f"Under {barrier} requires Red and Yellow among digits {boundary}–9."
    if not ranks.green.issubset(green_blue_zone) or not ranks.blue.issubset(green_blue_zone):
        return False, f"Under {barrier} requires Green and Blue among digits 0–{max(barrier - 2, 0)}."
    if any(float(percentages[d]) >= OVER_RED_YELLOW_MAX_PERCENT for d in (ranks.red | ranks.yellow)):
        return False, f"Under {barrier} requires both Red and Yellow percentages to be strictly below 9%."
    return True, f"Under {barrier} colour positions pass; Red and Yellow are <9%."


def evaluate_stability(
    observations: Sequence[Mapping[int, float]],
    group: Group | None = None,
    max_spread: float | None = None,
) -> tuple[bool, str]:
    """Check unchanged Red/Green identity sets, not percentage spread."""
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


def _trigger_digit(
    percentages: Mapping[int, float], group: Group, barrier: int
) -> int | None:
    digits = digits_for(group, barrier)
    values = {digit: float(percentages[digit]) for digit in digits}
    ordered_levels = sorted(set(values.values()))
    if not ordered_levels:
        return None
    # Both directions monitor either of the two least-frequency levels. The
    # session tracks every candidate at those levels.
    candidate_levels = set(ordered_levels[:2])
    candidates = [digit for digit, value in values.items() if value in candidate_levels]
    return min(candidates) if candidates else None


def evaluate_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
    group: Group,
    barrier: int = DEFAULT_BARRIER,
) -> GroupEvaluation:
    if not strategy_profile_supported(group, barrier):
        reason = f"{group.value.title()} {barrier} has no complete approved strategy profile; observation only."
        return GroupEvaluation(group, False, False, False, False, False, reason)
    threshold_ok, threshold_reason = evaluate_thresholds(percentages, group, barrier)
    stability_ok, stability_reason = evaluate_stability(observations)
    color_ok, color_reason = evaluate_color_positions(percentages, group, barrier)
    trigger_digit = (
        _trigger_digit(percentages, group, barrier)
        if validate_barrier(barrier) and barrier_valid_for(group, barrier) and _valid_percentage_map(percentages)
        else None
    )
    trigger_ok = trigger_digit is not None
    trigger_reason = (
        f"Unique trigger digit {trigger_digit} selected from the {group.value.title()} {barrier} digit range."
        if trigger_ok
        else "Trigger blocked: the barrier range is invalid or its lowest-frequency digit is tied."
    )
    qualifies = threshold_ok and stability_ok and color_ok and trigger_ok
    return GroupEvaluation(
        group, threshold_ok, stability_ok, color_ok, trigger_ok, qualifies,
        " ".join((threshold_reason, stability_reason, color_reason, trigger_reason)),
    )


def choose_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
    barrier: int = DEFAULT_BARRIER,
) -> QualificationResult:
    """Automatically select only the sole qualifying direction for this barrier."""
    invalid_under = GroupEvaluation(Group.UNDER, False, False, False, False, False, "Incomplete or invalid data.")
    invalid_over = GroupEvaluation(Group.OVER, False, False, False, False, False, "Incomplete or invalid data.")
    if not _valid_percentage_map(percentages):
        return QualificationResult(
            Decision.INVALID_DATA, invalid_under, invalid_over,
            reason="All ten current digit percentages must be finite values from 0 to 100.",
        )
    if not validate_barrier(barrier):
        return QualificationResult(
            Decision.INVALID_DATA, invalid_under, invalid_over,
            reason="Selected barrier must be an integer digit from 0 to 9.",
        )
    if not (
        strategy_profile_supported(Group.UNDER, barrier)
        or strategy_profile_supported(Group.OVER, barrier)
    ):
        reason = f"Barrier {barrier} has no valid activity/confirmation range for either direction; observation only."
        unsupported_under = GroupEvaluation(Group.UNDER, False, False, False, False, False, reason)
        unsupported_over = GroupEvaluation(Group.OVER, False, False, False, False, False, reason)
        return QualificationResult(
            Decision.OBSERVE, unsupported_under, unsupported_over, reason=reason
        )

    under = evaluate_group(percentages, observations, Group.UNDER, barrier)
    over = evaluate_group(percentages, observations, Group.OVER, barrier)
    if under.qualifies and over.qualifies:
        return QualificationResult(
            Decision.WAIT_BOTH_QUALIFY, under, over,
            reason="Both directions qualify for the selected barrier; continue observing instead of choosing arbitrarily.",
        )
    if not under.qualifies and not over.qualifies:
        return QualificationResult(
            Decision.OBSERVE, under, over,
            reason=f"Neither direction qualifies for barrier {barrier}; continue observing.",
        )

    group = Group.UNDER if under.qualifies else Group.OVER
    digit = _trigger_digit(percentages, group, barrier)
    if digit is None:
        return QualificationResult(
            Decision.OBSERVE, under, over,
            reason="The lowest-frequency digit in the selected barrier range is tied; continue observing.",
        )
    decision = Decision.SELECT_UNDER if group is Group.UNDER else Decision.SELECT_OVER
    return QualificationResult(
        decision, under, over, trigger_digit=digit,
        reason=f"{group.value.title().title()} {barrier} alone qualifies; direction selected automatically.",
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
    barrier: int = DEFAULT_BARRIER,
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
    return choose_group(snapshots[-1], snapshots, barrier)


@dataclass
class PatternTracker:
    """After a percentage trigger, require one or more consecutive activity digits then confirmation.

    The trigger is a percentage increase, not the trigger digit appearing as a
    tick. The caller arms this tracker only after the trigger percentage rises.
    """
    selected_group: Group
    trigger_digit: int
    barrier: int | None = None
    required_activity_ticks: int = MIN_ACTIVITY_TICKS
    progress: int = 0
    completed_patterns: int = 0
    armed: bool = False

    def __post_init__(self) -> None:
        if self.barrier is None:
            self.barrier = DEFAULT_BARRIER
        if not validate_barrier(self.barrier) or not barrier_valid_for(self.selected_group, self.barrier):
            raise ValueError("Barrier is not valid for the selected direction.")
        if type(self.trigger_digit) is not int or self.trigger_digit not in digits_for(self.selected_group, self.barrier):
            raise ValueError("Trigger digit must belong to the selected barrier's digit range.")
        if type(self.required_activity_ticks) is not int or self.required_activity_ticks < MIN_ACTIVITY_TICKS:
            raise ValueError("At least one activity tick is required before confirmation.")

    def arm(self) -> None:
        self.progress = 0
        self.armed = True

    def reset(self) -> None:
        self.progress = 0
        self.armed = False

    def on_tick(self, digit: int) -> bool:
        if type(digit) is not int or digit not in ALL_DIGITS:
            raise ValueError("Last digit must be an integer from 0 to 9.")
        if not self.armed:
            return False

        activity = activity_digits_for(self.selected_group, self.barrier)
        confirmation = confirmation_digits_for(self.selected_group, self.barrier)
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

        # Digits outside the activity/confirmation bands break the sequence.
        self.progress = 0
        return False
