from collections import Counter
from typing import Sequence

import numpy as np


def analyze_digits(digits: Sequence[int]) -> dict:
    """Suemarize obserced lats digits from 0 through 9."""
    if not digits:
        raise ValueError("Provide at least 1 digit.")
    if any(not isinstance(d, (int, np.integer)) or d < 0 or d > 9 for d in digits):
        raise ValueError("Every digit must be an integer  from 0 to 9.")
    counts = Counter(int(d) for d in digits)
    total = len(digits)

    frequencies = {
        str(d): counts[d]/total
        for d in range(10)

    }
    expected = total / 10
    chi_square = sum(
        (counts[d] - expected) **2 / expected
        for d in range(10)

    )

    return{
        "sample_size": total,
        "counts":{str(d): counts[d] for d in range(10)},
        "frequencies": frequencies,
        "most_observed_digit": max(range(10), key=lambda d: counts[d]),
        "least_observed_digit": min(range(10), key=lambda d: counts[d]),
        "chi_square_statistic": round(chi_square, 4),
        "warning": (
            "Historical frequency is descriptive, not proof of a "
            "predictive edge. Statistical significance requires further testing."
        ),    
    }
