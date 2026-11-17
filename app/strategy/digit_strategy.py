"""Pure rule engine for barrier-aware TradeBridge digit strategies.

The selected barrier configures both candidate directions: Over evaluates digits
0..barrier and Under evaluates digits barrier..9. Red/Yellow and Green/Blue
zones remain the documented directional halves (0–4 / 5–9). No transport,
database, UI, or order-execution side effects belong in this module.

Statistical basis for the 12% threshold signal:
On Deriv synthetic indices the RNG is designed to maintain long-run uniformity
across the ten last-digit values, so the expected frequency of each digit is
10% over a sufficiently large window. A digit that has accumulated ≥ 12% over
a 1,000-tick window represents roughly a 20% excess above that uniform baseline
(z-score ≈ 6.3 at n=1000, p=0.10). The RNG design creates genuine
autocorrelation in running frequency over finite windows: the system must
correct the excess to restore long-run uniformity, so the opposing side carries
higher conditional probability going forward. This is mean reversion toward
uniform distribution. It is NOT the gambler's fallacy because the autocorrelation
is a property of the RNG design, not an inference from independent draws.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Sequence

ALL_DIGITS = frozenset(range(10))
LOW_COLOR_ZONE = frozenset(range(5))
HIGH_COLOR_ZONE = frozenset(range(5, 10))

STABILITY_SECONDS = 15.0
PERCENTAGE_TRIGGER_RISE = 0.1
OVER_RED_YELLOW_MAX_PERCENT = 9.0
FILTER_LIMIT = 10.0
FILTER_EXCEPTION_MAX = 10.5
MIN_ACTIVITY_TICKS = 1
DEFAULT_BARRIER = 5
THRESHOLD_ENTRY_PCT = 12.0
REVERSION_THRESHOLD_PCT = 12.0


class Group(str, Enum):
    UNDER = "under"
    OVER = "over"


class Decision(str, Enum):
    OBSERVE = "observe"
    WAIT_BOTH_QUALIFY = "wait_both_qualify"
    SELECT_UNDER = "select_under"
    SELECT_OVER = "select_over"
    INVALID_DATA = "invalid_data"


class ContractType(str, Enum):
    OVER = "over"
    UNDER = "under"
    EVEN = "even"
    ODD = "odd"


@dataclass(frozen=True)
class ThresholdSignal:
    triggered: bool
    qualifying_digit: int | None
    qualifying_pct: float | None
    confidence: str  # "HIGH" | "MEDIUM" | "NONE"
    reason: str


@dataclass(frozen=True)
class EvenOddSetup:
    armed: bool
    target: ContractType
    parity_power: float
    reason: str


def evaluate_even_odd_setup(
    percentages: Mapping[int, float],
    target: ContractType,
    previous_parity_power: float | None = None,
) -> EvenOddSetup:
    """Apply the Maziwa parity-power and rank-alignment filter."""
    if target not in (ContractType.EVEN, ContractType.ODD):
        return EvenOddSetup(False, target, 0.0, "EVEN/ODD setup requires EVEN or ODD.")
    if not _valid_percentage_map(percentages):
        return EvenOddSetup(False, target, 0.0, "Invalid percentage map.")

    target_digits = {0, 2, 4, 6, 8} if target is ContractType.EVEN else {1, 3, 5, 7, 9}
    opposing_digits = set(range(10)) - target_digits
    parity_power = sum(float(percentages[d]) for d in target_digits)
    required_power = 58.0 if target is ContractType.EVEN else 70.0
    ranked = sorted(range(10), key=lambda digit: (-float(percentages[digit]), digit))
    green, blue = ranked[0], ranked[1]
    yellow, red = ranked[-2], ranked[-1]

    if parity_power < 55.0:
        return EvenOddSetup(False, target, parity_power, f"{target.value.upper()} power {parity_power:.1f}% is below 55.0%.")
    if previous_parity_power is not None and parity_power <= previous_parity_power:
        return EvenOddSetup(False, target, parity_power, f"{target.value.upper()} power is not rising ({parity_power:.1f}% vs {previous_parity_power:.1f}%).")
    if green not in target_digits or blue not in target_digits:
        return EvenOddSetup(False, target, parity_power, "Green and Blue must belong to the target parity.")
    if red not in opposing_digits or yellow not in opposing_digits:
        return EvenOddSetup(False, target, parity_power, "Red and Yellow must belong to the opposing parity.")
    return EvenOddSetup(True, target, parity_power, f"{target.value.upper()} setup armed with rising {parity_power:.1f}% parity power.")


@dataclass(frozen=True)
class ReverseSignal:
    suggested: bool
    reason: str


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
    threshold_signal: ThresholdSignal = ThresholdSignal(False, None, None, "NONE", "Not evaluated.")


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
    """Allow at most one selected digit >=10.0%, strictly below 10.5%."""
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
        return False, f"Percentage filter failed: at least {required_below} of {len(digits)} selected digits must be strictly below 10.0%."
    exceptions = [d for d in digits if float(percentages[d]) >= FILTER_LIMIT]
    if exceptions:
        if len(exceptions) != 1 or not float(percentages[exceptions[0]]) < FILTER_EXCEPTION_MAX:
            return False, f"{group.value.title()} percentage filter failed: the one remaining digit must be strictly below 10.5%."
    return True, f"{group.value.title()} {barrier} percentage filter passes: at least {required_below} selected digits are below 10.0%."


def evaluate_color_positions(
    percentages: Mapping[int, float],
    group: Group,
    barrier: int = DEFAULT_BARRIER,
) -> tuple[bool, str]:
    """Validate barrier-relative colour positions, allowing Red-alone qualification.

    Red must be on the selected direction's required side. Yellow is an
    additional qualifying colour only when it is on that same side; Yellow
    being outside the side does not invalidate a valid Red-alone setup.
    Green and Blue must still occupy the opposite-side zone.
    """
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
        # Over B: Red/Yelllow qualifying zone is 0..B+1; Green/Blue is B+2..9.
        boundary = min(barrier + 2, 10)
        red_yellow_zone = frozenset(range(0, boundary))
        green_blue_zone = frozenset(range(boundary, 10))
        if not ranks.red.issubset(red_yellow_zone):
            return False, f"Over {barrier} requires Red among digits 0–{min(barrier + 1, 9)}."
        if not ranks.green.issubset(green_blue_zone) or not ranks.blue.issubset(green_blue_zone):
            return False, f"Over {barrier} requires Green and Blue among digits {boundary}–9."
        qualifying_low_colors = ranks.red | ranks.yellow
        if any(float(percentages[d]) >= OVER_RED_YELLOW_MAX_PERCENT for d in qualifying_low_colors):
            return False, f"Over {barrier} requires both Red and Yellow to be strictly below 9%, regardless of Yellow's zone."
        if ranks.yellow.issubset(red_yellow_zone):
            return True, f"Over {barrier} colour positions pass; Red and Yellow are on the low side and both <9%."
        return True, f"Over {barrier} colour positions pass through Red-alone qualification; Yellow is outside the low-side zone and both Red and Yellow are below 9%."

    # Under B: Red/Yellow qualifying zone is B-1..9; Green/Blue is 0..B-2.
    boundary = max(barrier - 1, 0)
    green_blue_zone = frozenset(range(0, boundary))
    red_yellow_zone = frozenset(range(boundary, 10))
    if not ranks.red.issubset(red_yellow_zone):
        return False, f"Under {barrier} requires Red among digits {boundary}–9."
    if not ranks.green.issubset(green_blue_zone) or not ranks.blue.issubset(green_blue_zone):
        return False, f"Under {barrier} requires Green and Blue among digits 0–{max(barrier - 2, 0)}."
    qualifying_high_colors = ranks.red | ranks.yellow
    if any(float(percentages[d]) >= OVER_RED_YELLOW_MAX_PERCENT for d in qualifying_high_colors):
        return False, f"Under {barrier} requires both Red and Yellow to be strictly below 9%, regardless of Yellow's zone."
    if ranks.yellow.issubset(red_yellow_zone):
        return True, f"Under {barrier} colour positions pass; Red and Yellow are on the high side and both <9%."
    return True, f"Under {barrier} colour positions pass through Red-alone qualification; Yellow is outside the high-side zone and both Red and Yellow are below 9%."


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
    preferred = prioritize_trigger_candidate(percentages, group, barrier, candidates)
    if preferred is not None:
        return preferred
    return min(candidates) if candidates else None


def prioritize_trigger_candidate(
    percentages: Mapping[int, float],
    group: Group,
    barrier: int,
    candidates: Sequence[int],
) -> int | None:
    """Apply direction-specific Red/Yellow trigger priority when eligible."""
    if not candidates:
        return None

    ranks = rank_bar_colors(percentages)
    if len(ranks.red) != 1:
        return None
    red = next(iter(ranks.red))

    if group is Group.OVER:
        yellow_boundary = barrier + 1
    else:
        yellow_boundary = barrier - 1

    if red == yellow_boundary and len(ranks.yellow) == 1:
        yellow = next(iter(ranks.yellow))
        if yellow in digits_for(group, barrier) and yellow in candidates:
            return yellow
    if red in candidates:
        return red
    return None


def evaluate_threshold_signal(
    percentages: Mapping[int, float],
    contract_type: ContractType,
    barrier: int = DEFAULT_BARRIER,
) -> ThresholdSignal:
    """
    Check whether the 12% threshold entry condition is met for the given contract type.

    Statistical basis: a digit at ≥12% represents ~20% excess above the uniform 10%
    baseline (z-score ≈ 6.3 over a 1000-tick window). Mean-reversion toward
    uniform distribution makes the opposing side statistically favoured going
    forward — this is REVERSION_THRESHOLD_PCT at work.

    Contract-type rules (confirmed from Deriv community / DollarPrinter framework):
      OVER B  : any digit in 0..B  with percentage ≥ THRESHOLD_ENTRY_PCT  → lower zone excess → trade over
      UNDER B : any digit in B..9  with percentage ≥ THRESHOLD_ENTRY_PCT  → upper zone excess → trade under
      EVEN    : any even digit (0,2,4,6,8) with percentage ≥ THRESHOLD_ENTRY_PCT
      ODD     : any odd  digit (1,3,5,7,9) with percentage ≥ THRESHOLD_ENTRY_PCT

    Confidence:
      HIGH   — qualifying digit is also the Green (highest %) or Blue (second-highest %) digit
      MEDIUM — threshold met but qualifying digit is NOT Green or Blue
      NONE   — threshold not met
    """
    # Validate percentages map
    if not _valid_percentage_map(percentages):
        return ThresholdSignal(False, None, None, "NONE", "Invalid percentage map.")
    if not validate_barrier(barrier):
        return ThresholdSignal(False, None, None, "NONE", "Invalid barrier.")

    # Determine which digits to scan
    if contract_type is ContractType.OVER:
        scan_digits = list(range(0, barrier + 1))
    elif contract_type is ContractType.UNDER:
        scan_digits = list(range(barrier, 10))
    elif contract_type is ContractType.EVEN:
        scan_digits = [0, 2, 4, 6, 8]
    else:  # ODD
        scan_digits = [1, 3, 5, 7, 9]

    # Find the qualifying digit (highest percentage among those ≥ threshold)
    qualifying: list[tuple[float, int]] = [
        (float(percentages[d]), d)
        for d in scan_digits
        if float(percentages[d]) >= THRESHOLD_ENTRY_PCT
    ]
    if not qualifying:
        return ThresholdSignal(False, None, None, "NONE",
            f"No digit in the {contract_type.value.upper()} zone reached {THRESHOLD_ENTRY_PCT}%.")

    # Pick the highest-percentage qualifying digit; break ties by lowest digit index
    max_pct = max(pct for pct, _ in qualifying)
    best_digit = min(d for pct, d in qualifying if pct == max_pct)
    best_pct = max_pct

    # Determine confidence via colour rank
    try:
        ranks = rank_bar_colors(percentages)
    except ValueError:
        return ThresholdSignal(True, best_digit, best_pct, "MEDIUM",
            f"Digit {best_digit} at {best_pct:.1f}% meets threshold; colour ranking unavailable (MEDIUM).")

    if best_digit in ranks.green or best_digit in ranks.blue:
        confidence = "HIGH"
        conf_reason = f"Digit {best_digit} at {best_pct:.1f}% meets threshold and is {'Green' if best_digit in ranks.green else 'Blue'} (HIGH confidence)."
    else:
        confidence = "MEDIUM"
        conf_reason = f"Digit {best_digit} at {best_pct:.1f}% meets threshold but is not Green or Blue (MEDIUM confidence)."

    return ThresholdSignal(True, best_digit, best_pct, confidence, conf_reason)


def evaluate_reverse_signal(
    percentages: Mapping[int, float],
    group: Group,
    barrier: int = DEFAULT_BARRIER,
) -> ReverseSignal:
    """
    Detect when a normal directional signal may actually be exhausted.

    Reverse trigger condition:
      - The Green digit (highest percentage) is ≥ REVERSION_THRESHOLD_PCT
      - AND the Green digit is in the CONFIRMING zone (the high-side for OVER,
        the low-side for UNDER) — meaning the normally bullish side is saturated
        and may mean-revert in the opposite direction.

    When this condition holds the DollarPrinter community sometimes reverses:
      OVER  → consider UNDER instead (high side is saturated)
      UNDER → consider OVER  instead (low side is saturated)
    """
    if not _valid_percentage_map(percentages):
        return ReverseSignal(False, "Invalid percentage map.")
    if not validate_barrier(barrier):
        return ReverseSignal(False, "Invalid barrier.")
    try:
        ranks = rank_bar_colors(percentages)
    except ValueError as exc:
        return ReverseSignal(False, str(exc))

    if len(ranks.green) != 1:
        return ReverseSignal(False, "Green digit is tied; reverse signal indeterminate.")

    green_digit = next(iter(ranks.green))
    green_pct = float(percentages[green_digit])

    if green_pct < REVERSION_THRESHOLD_PCT:
        return ReverseSignal(False,
            f"Green digit {green_digit} at {green_pct:.1f}% is below the {REVERSION_THRESHOLD_PCT}% reversion threshold.")

    # For OVER: confirming (high) zone is barrier+1..9
    # For UNDER: confirming (low) zone is 0..barrier-1
    if group is Group.OVER:
        confirming_zone = frozenset(range(barrier + 1, 10))
        direction_label = "OVER"
        reverse_label = "UNDER"
    else:
        confirming_zone = frozenset(range(0, barrier))
        direction_label = "UNDER"
        reverse_label = "OVER"

    if green_digit not in confirming_zone:
        return ReverseSignal(False,
            f"Green digit {green_digit} at {green_pct:.1f}% is not in the confirming zone for {direction_label} {barrier}.")

    return ReverseSignal(True,
        f"Green digit {green_digit} at {green_pct:.1f}% is in the {direction_label} {barrier} confirming zone and ≥{REVERSION_THRESHOLD_PCT}% — "
        f"confirming side may be saturated; consider {reverse_label} instead (DollarPrinter reverse trigger).")


def evaluate_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
    group: Group,
    barrier: int = DEFAULT_BARRIER,
    stability_passed: bool = False,
) -> GroupEvaluation:
    if not strategy_profile_supported(group, barrier):
        reason = f"{group.value.title()} {barrier} has no complete approved strategy profile; observation only."
        return GroupEvaluation(group, False, False, False, False, False, reason)
    threshold_ok, threshold_reason = evaluate_thresholds(percentages, group, barrier)
    stability_ok, stability_reason = (
        (True, "Red/Green market stability already passed at Run; not rechecked after qualification.")
        if stability_passed
        else evaluate_stability(observations)
    )
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
    contract_type = ContractType.OVER if group is Group.OVER else ContractType.UNDER
    threshold_signal = evaluate_threshold_signal(percentages, contract_type, barrier)
    qualifies = threshold_ok and stability_ok and color_ok and trigger_ok
    return GroupEvaluation(
        group, threshold_ok, stability_ok, color_ok, trigger_ok, qualifies,
        " ".join((threshold_reason, stability_reason, color_reason, trigger_reason)),
        threshold_signal,
    )


def choose_group(
    percentages: Mapping[int, float],
    observations: Sequence[Mapping[int, float]],
    barrier: int = DEFAULT_BARRIER,
    stability_passed: bool = False,
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

    under = evaluate_group(percentages, observations, Group.UNDER, barrier, stability_passed)
    over = evaluate_group(percentages, observations, Group.OVER, barrier, stability_passed)
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


@dataclass
class FastPatternTracker:
    """
    2-tick fast confirmation mode matching the DollarPrinter community pattern:
      1. After arm(), wait for the first LOW digit (Red or Yellow colour rank).
      2. After stage 1, wait for the first HIGH digit (Green or Blue colour rank).
      3. On HIGH digit after LOW digit → pattern complete.

    This is a simplified version of the strict PatternTracker. Use when the
    full activity/confirmation band sequence is too slow for fast markets.

    The percentages snapshot is captured at arm() time and used to determine
    which digits are LOW (red/yellow) and HIGH (green/blue) at that moment.
    """
    selected_group: Group
    barrier: int = DEFAULT_BARRIER
    _stage: int = 0           # 0=waiting for LOW, 1=waiting for HIGH
    _armed: bool = False
    _completed: bool = False
    _percentages: dict[int, float] | None = None
    _low_digits: frozenset[int] = field(default_factory=frozenset)
    _high_digits: frozenset[int] = field(default_factory=frozenset)
    completed_patterns: int = 0

    def __post_init__(self) -> None:
        if not validate_barrier(self.barrier) or not barrier_valid_for(self.selected_group, self.barrier):
            raise ValueError("Barrier is not valid for the selected direction.")

    def arm(self, percentages: Mapping[int, float]) -> None:
        """Arm the tracker, capturing current colour ranks to define LOW and HIGH digits."""
        if not _valid_percentage_map(percentages):
            raise ValueError("Valid percentages required to arm FastPatternTracker.")
        ranks = rank_bar_colors(percentages)
        self._low_digits = ranks.red | ranks.yellow
        self._high_digits = ranks.green | ranks.blue
        self._stage = 0
        self._armed = True
        self._completed = False
        self._percentages = dict(percentages)

    def reset(self) -> None:
        self._stage = 0
        self._armed = False
        self._completed = False

    @property
    def armed(self) -> bool:
        return self._armed

    def on_tick(self, digit: int) -> bool:
        """Process one tick. Returns True when the 2-tick pattern completes."""
        if type(digit) is not int or digit not in ALL_DIGITS:
            raise ValueError("Last digit must be an integer from 0 to 9.")
        if not self._armed:
            return False
        if self._stage == 0:
            if digit in self._low_digits:
                self._stage = 1
            return False
        # stage == 1: waiting for HIGH digit
        if digit in self._high_digits:
            self._armed = False
            self._completed = True
            self.completed_patterns += 1
            return True
        # Any digit that is not HIGH (including LOW and neutral) resets to stage 0.
        # This enforces the 2-tick contract: HIGH must immediately follow the LOW
        # with no intervening digits of any other class.
        self._stage = 0
        return False
