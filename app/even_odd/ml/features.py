"""Leakage-safe supervised feature generation from ordered last digits.

For every row, all features are computed strictly from ticks before the target
tick. The target parity is stored separately and must never be included in the
model feature matrix.
"""

from collections import Counter
from math import log2
from typing import Sequence


DEFAULT_WINDOWS = (50, 100, 250, 500, 1000)


def _validate_digits(digits: Sequence[int]) -> None:
    if any(type(digit) is not int or not 0 <= digit <= 9 for digit in digits):
        raise ValueError("Every last digit must be an integer from 0 to 9.")


def _window_features(window: Sequence[int], prefix: str) -> dict[str, float]:
    total = len(window)
    counts = Counter(window)
    even_count = sum(1 for digit in window if digit % 2 == 0)
    odd_count = total - even_count
    features: dict[str, float] = {
        f"{prefix}_even_rate": even_count / total,
        f"{prefix}_odd_rate": odd_count / total,
        f"{prefix}_parity_imbalance": (even_count - odd_count) / total,
    }

    entropy = 0.0
    for digit in range(10):
        rate = counts[digit] / total
        features[f"{prefix}_digit_{digit}_rate"] = rate
        if rate > 0:
            entropy -= rate * log2(rate)
    features[f"{prefix}_digit_entropy"] = entropy / log2(10)

    transition_counts = Counter()
    for previous, current in zip(window, window[1:]):
        key = ("even" if previous % 2 == 0 else "odd",
               "even" if current % 2 == 0 else "odd")
        transition_counts[key] += 1
    transition_total = max(1, total - 1)
    for source in ("even", "odd"):
        for target in ("even", "odd"):
            features[f"{prefix}_{source}_to_{target}_rate"] = (
                transition_counts[(source, target)] / transition_total
            )

    latest_parity = window[-1] % 2
    streak = 0
    for digit in reversed(window):
        if digit % 2 != latest_parity:
            break
        streak += 1
    features[f"{prefix}_current_streak_length"] = float(streak)
    features[f"{prefix}_latest_parity_even"] = float(latest_parity == 0)
    for digit in range(10):
        features[f"{prefix}_latest_digit_{digit}"] = float(window[-1] == digit)

    # Compare the most recent half-window with the preceding half-window.
    half = max(1, total // 2)
    recent = window[-half:]
    previous = window[-2 * half:-half]
    if previous:
        recent_even_rate = sum(d % 2 == 0 for d in recent) / len(recent)
        previous_even_rate = sum(d % 2 == 0 for d in previous) / len(previous)
        features[f"{prefix}_even_rate_momentum"] = recent_even_rate - previous_even_rate
    else:
        features[f"{prefix}_even_rate_momentum"] = 0.0

    return features


def build_supervised_rows(
    digits: Sequence[int],
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> list[dict]:
    """Build chronological rows with strictly pre-target features.

    Each output row contains:
      - target_index: index of the next tick in the supplied sequence;
      - features: numeric features calculated from earlier ticks only;
      - target_parity: the next tick's parity label.

    A row is emitted only when every requested feature window is available.
    """
    _validate_digits(digits)
    if not windows or any(type(size) is not int or size < 2 for size in windows):
        raise ValueError("windows must contain positive integer sizes of at least 2.")
    if len(set(windows)) != len(windows):
        raise ValueError("Feature windows must be unique.")

    minimum_history = max(windows)
    if len(digits) <= minimum_history:
        return []

    rows = []
    for target_index in range(minimum_history, len(digits)):
        history = digits[:target_index]
        features: dict[str, float] = {}
        for size in windows:
            features.update(_window_features(history[-size:], f"w{size}"))

        # Target is intentionally separate from features; never use it to
        # calculate any rolling statistic for this row.
        rows.append({
            "target_index": target_index,
            "features": features,
            "target_parity": "even" if digits[target_index] % 2 == 0 else "odd",
        })
    return rows



def chronological_split(
    rows: Sequence[dict],
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> dict[str, list[dict]]:
    """Split ordered samples without shuffling or crossing chronology.

    The remaining fraction is reserved for the final untouched test set.
    """
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1.")
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1.")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("Train and validation fractions must leave a test set.")
    if len(rows) < 5:
        raise ValueError("At least five supervised rows are required to split.")
    indices = [row.get("target_index") for row in rows]
    if any(type(index) is not int for index in indices):
        raise ValueError("Every row must contain an integer target_index.")
    if indices != sorted(indices) or len(set(indices)) != len(indices):
        raise ValueError("Rows must be uniquely ordered by target_index.")

    train_end = int(len(rows) * train_fraction)
    validation_end = train_end + int(len(rows) * validation_fraction)
    if train_end < 1 or validation_end <= train_end or validation_end >= len(rows):
        raise ValueError("Split fractions produce an empty train, validation, or test set.")

    return {
        "train": list(rows[:train_end]),
        "validation": list(rows[train_end:validation_end]),
        "test": list(rows[validation_end:]),
    }
