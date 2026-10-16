from collections import Counter
from typing import Sequence


def analyze_digits(digits: Sequence[int]) -> dict:
    """Summarize observed last digits (0 through 9) without claiming predictability."""
    if not digits:
        raise ValueError("Provide at least one digit.")

    # bool is a subclass of int in Python, so reject it explicitly.
    if any(type(digit) is not int or not 0 <= digit <= 9 for digit in digits):
        raise ValueError("Every digit must be an integer from 0 to 9.")

    counts = Counter(digits)
    total = len(digits)
    frequencies = {str(digit): counts[digit] / total for digit in range(10)}
    expected = total / 10
    chi_square = sum(
        (counts[digit] - expected) ** 2 / expected
        for digit in range(10)
    )

    return {
        "sample_size": total,
        "counts": {str(digit): counts[digit] for digit in range(10)},
        "frequencies": frequencies,
        "most_observed_digits": [
            digit for digit in range(10)
            if counts[digit] == max(counts.values())
        ],
        "least_observed_digits": [
            digit for digit in range(10)
            if counts[digit] == min(counts.values())
        ],
        "chi_square_statistic": round(chi_square, 4),
        "interpretation": (
            "Descriptive statistics only. This result does not establish a "
            "predictive edge; test hypotheses on sufficiently large, "
            "time-separated out-of-sample data."
        ),
    }
