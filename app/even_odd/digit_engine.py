"""Descriptive Even/Odd and last-digit analytics.

No predictive claims are made here. All outputs describe observations already
received and are suitable as inputs to a separately validated ML pipeline.
"""

from collections import Counter
from typing import Sequence


PARITY_SEQUENCE_LENGTHS = (2, 3, 4, 5)


def _parity(digit: int) -> str:
    return "even" if digit % 2 == 0 else "odd"


def _percentage(values: Sequence[int], predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if predicate(value)) / len(values) * 100.0


def _movement_summary(digits: Sequence[int]) -> dict:
    """Return recent-versus-prior percentage movement in percentage points."""
    total = len(digits)
    midpoint = total // 2
    previous_half = digits[:midpoint]
    recent_half = digits[midpoint:]

    previous_even = _percentage(previous_half, lambda digit: digit % 2 == 0)
    recent_even = _percentage(recent_half, lambda digit: digit % 2 == 0)
    has_comparison_period = bool(previous_half)
    even_momentum = recent_even - previous_even if has_comparison_period else 0.0
    digit_momentum = {
        str(digit): round(
            (
                _percentage(recent_half, lambda value, d=digit: value == d)
                - _percentage(previous_half, lambda value, d=digit: value == d)
            ) if has_comparison_period else 0.0,
            2,
        )
        for digit in range(10)
    }

    if total >= 4:
        segments = [
            digits[(index * total) // 4:((index + 1) * total) // 4]
            for index in range(4)
        ]
        even_rates = [
            _percentage(segment, lambda digit: digit % 2 == 0)
            for segment in segments
        ]
        even_acceleration = (
            (even_rates[3] - even_rates[2])
            - (even_rates[1] - even_rates[0])
        )
        digit_acceleration = {}
        for digit in range(10):
            rates = [
                _percentage(segment, lambda value, d=digit: value == d)
                for segment in segments
            ]
            digit_acceleration[str(digit)] = round(
                (rates[3] - rates[2]) - (rates[1] - rates[0]), 2
            )
    else:
        even_acceleration = 0.0
        digit_acceleration = {str(digit): 0.0 for digit in range(10)}

    return {
        "even_percentage_momentum_pp": round(even_momentum, 2),
        "odd_percentage_momentum_pp": round(-even_momentum, 2),
        "digit_percentage_momentum_pp": digit_momentum,
        "even_percentage_acceleration_pp": round(even_acceleration, 2),
        "digit_percentage_acceleration_pp": digit_acceleration,
        "movement_comparison": {
            "previous_half_size": len(previous_half),
            "recent_half_size": len(recent_half),
        },
    }


def _transition_summary(digits: Sequence[int]) -> dict:
    """Return full 10x10 digit transitions and conditional transition rates."""
    digit_counts = {
        str(source): {str(target): 0 for target in range(10)}
        for source in range(10)
    }
    source_counts = {str(source): 0 for source in range(10)}
    parity_counts = {
        source: {target: 0 for target in ("even", "odd")}
        for source in ("even", "odd")
    }
    parity_source_counts = {source: 0 for source in ("even", "odd")}

    for previous, current in zip(digits, digits[1:]):
        digit_counts[str(previous)][str(current)] += 1
        source_counts[str(previous)] += 1
        source_parity = _parity(previous)
        target_parity = _parity(current)
        parity_counts[source_parity][target_parity] += 1
        parity_source_counts[source_parity] += 1

    digit_probabilities = {
        source: {
            target: round(count / source_counts[source], 6)
            if source_counts[source] else 0.0
            for target, count in targets.items()
        }
        for source, targets in digit_counts.items()
    }
    parity_probabilities = {
        source: {
            target: round(count / parity_source_counts[source], 6)
            if parity_source_counts[source] else 0.0
            for target, count in targets.items()
        }
        for source, targets in parity_counts.items()
    }

    return {
        "digit_transitions": {
            "source_counts": source_counts,
            "counts": digit_counts,
            "conditional_probabilities": digit_probabilities,
        },
        "parity_transition_counts": parity_counts,
        "parity_transition_source_counts": parity_source_counts,
        "parity_transition_conditional_probabilities": parity_probabilities,
    }


def _sequence_summary(digits: Sequence[int]) -> dict:
    parity_values = tuple(digit % 2 for digit in digits)
    patterns = {}
    for length in PARITY_SEQUENCE_LENGTHS:
        if len(parity_values) < length:
            patterns[str(length)] = {
                "pattern": None,
                "occurrences": 0,
                "rate": 0.0,
            }
            continue
        suffix = parity_values[-length:]
        occurrences = sum(
            1
            for index in range(len(parity_values) - length + 1)
            if parity_values[index:index + length] == suffix
        )
        patterns[str(length)] = {
            "pattern": "".join("E" if value == 0 else "O" for value in suffix),
            "occurrences": occurrences,
            "rate": round(occurrences / (len(parity_values) - length + 1), 6),
        }
    return {"latest_parity_suffix_patterns": patterns}


def _summary(digits: Sequence[int]) -> dict:
    total = len(digits)
    counts = Counter(digits)
    even_count = sum(1 for digit in digits if digit % 2 == 0)
    odd_count = total - even_count

    transitions = {"even_to_even": 0, "even_to_odd": 0,
                   "odd_to_even": 0, "odd_to_odd": 0}
    for previous, current in zip(digits, digits[1:]):
        transitions[f"{_parity(previous)}_to_{_parity(current)}"] += 1

    current_parity = _parity(digits[-1])
    current_streak = 0
    current_digit_streak = 0
    for digit in reversed(digits):
        if _parity(digit) != current_parity:
            break
        current_streak += 1
    for digit in reversed(digits):
        if digit != digits[-1]:
            break
        current_digit_streak += 1

    longest_streak = {"even": 0, "odd": 0}
    longest_digit_streak = {str(digit): 0 for digit in range(10)}
    running_parity = None
    running_digit = None
    running_parity_length = 0
    running_digit_length = 0
    for digit in digits:
        parity = _parity(digit)
        if parity == running_parity:
            running_parity_length += 1
        else:
            running_parity = parity
            running_parity_length = 1
        longest_streak[parity] = max(
            longest_streak[parity], running_parity_length
        )

        if digit == running_digit:
            running_digit_length += 1
        else:
            running_digit = digit
            running_digit_length = 1
        longest_digit_streak[str(digit)] = max(
            longest_digit_streak[str(digit)], running_digit_length
        )

    result = {
        "sample_size": total,
        "even": {"count": even_count, "percentage": round(even_count / total * 100, 1)},
        "odd": {"count": odd_count, "percentage": round(odd_count / total * 100, 1)},
        "digit_counts": {str(digit): counts[digit] for digit in range(10)},
        "digit_percentages": {
            str(digit): round(counts[digit] / total * 100, 1)
            for digit in range(10)
        },
        "latest_digit": digits[-1],
        "latest_parity": current_parity,
        "current_parity_streak": current_streak,
        "current_digit_streak": current_digit_streak,
        "longest_parity_streaks": longest_streak,
        "longest_digit_streaks": longest_digit_streak,
        "transitions": transitions,
    }
    result.update(_movement_summary(digits))
    result.update(_transition_summary(digits))
    result.update(_sequence_summary(digits))
    return result


def analyze_digits(digits: Sequence[int]) -> dict:
    """Validate digits and return full-sample plus standard rolling summaries."""
    if not digits:
        raise ValueError("At least one last digit is required.")
    if any(type(digit) is not int or not 0 <= digit <= 9 for digit in digits):
        raise ValueError("Every last digit must be an integer from 0 to 9.")

    windows = {}
    for size in (50, 100, 250, 500, 1000):
        if len(digits) >= size:
            windows[str(size)] = _summary(digits[-size:])

    return {
        "sample": _summary(digits),
        "rolling_windows": windows,
        "interpretation": (
            "Descriptive statistics only. Frequency imbalance, percentage movement, "
            "transition rates or streaks do not establish that the next digit is "
            "predictable. Predictive value must be demonstrated on chronological "
            "out-of-sample data."
        ),
    }
