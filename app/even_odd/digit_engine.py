"""Descriptive Even/Odd and last-digit analytics.

No predictive claims are made here. All outputs describe observations already
received and are suitable as inputs to a separately validated ML pipeline.
"""

from collections import Counter
from typing import Sequence


def _parity(digit: int) -> str:
    return "even" if digit % 2 == 0 else "odd"


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
    for digit in reversed(digits):
        if _parity(digit) != current_parity:
            break
        current_streak += 1

    longest_streak = {"even": 0, "odd": 0}
    running_parity = None
    running_length = 0
    for digit in digits:
        parity = _parity(digit)
        if parity == running_parity:
            running_length += 1
        else:
            running_parity = parity
            running_length = 1
        longest_streak[parity] = max(longest_streak[parity], running_length)

    return {
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
        "longest_parity_streaks": longest_streak,
        "transitions": transitions,
    }


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
            "Descriptive statistics only. A frequency imbalance or streak does "
            "not establish that the next digit is predictable. Predictive value "
            "must be demonstrated on chronological out-of-sample data."
        ),
    }
