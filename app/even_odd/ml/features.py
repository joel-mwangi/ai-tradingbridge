"""Leakage-safe supervised feature generation from ordered last digits.

For every row, all features are computed strictly from ticks before the target
tick. The target parity is stored separately and must never be included in the
model feature matrix.

Feature groups include:
- rolling parity and individual-digit frequencies;
- frequency momentum and parity acceleration;
- parity and latest-digit-conditioned transitions;
- repeated suffix-pattern frequencies;
- cross-window parity and digit-frequency divergence.
"""

from collections import Counter
from math import log2
from typing import Sequence


DEFAULT_WINDOWS = (50, 100, 250, 500, 1000)
PARITY_SEQUENCE_LENGTHS = (2, 3, 4, 5)


def _validate_digits(digits: Sequence[int]) -> None:
    if any(type(digit) is not int or not 0 <= digit <= 9 for digit in digits):
        raise ValueError("Every last digit must be an integer from 0 to 9.")


def _validate_windows(windows: Sequence[int]) -> None:
    if not windows or any(type(size) is not int or size < 2 for size in windows):
        raise ValueError("windows must contain positive integer sizes of at least 2.")
    if len(set(windows)) != len(windows):
        raise ValueError("Feature windows must be unique.")


def _rate(values: Sequence[int], predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if predicate(value)) / len(values)


def _window_features(
    window: Sequence[int],
    prefix: str,
    *,
    include_latest_digit_identity: bool = False,
    include_latest_digit_transitions: bool = False,
    include_sequence_features: bool = False,
) -> dict[str, float]:
    """Calculate bounded descriptive features using only the supplied past window."""
    total = len(window)
    if total < 2:
        raise ValueError("A feature window must contain at least two digits.")

    counts = Counter(window)
    even_count = sum(1 for digit in window if digit % 2 == 0)
    even_rate = even_count / total
    odd_rate = 1.0 - even_rate

    features: dict[str, float] = {
        f"{prefix}_even_rate": even_rate,
        f"{prefix}_odd_rate": odd_rate,
        f"{prefix}_parity_imbalance": even_rate - odd_rate,
    }

    entropy = 0.0
    for digit in range(10):
        rate = counts[digit] / total
        features[f"{prefix}_digit_{digit}_rate"] = rate
        if rate > 0:
            entropy -= rate * log2(rate)
    features[f"{prefix}_digit_entropy"] = entropy / log2(10)

    # Parity transitions are exposed both as joint rates and as conditional
    # probabilities given the source parity.
    transition_counts = Counter()
    digit_transition_counts = Counter()
    for previous, current in zip(window, window[1:]):
        source_parity = "even" if previous % 2 == 0 else "odd"
        target_parity = "even" if current % 2 == 0 else "odd"
        transition_counts[(source_parity, target_parity)] += 1
        if include_latest_digit_transitions:
            digit_transition_counts[(previous, current)] += 1

    transition_total = max(1, total - 1)
    for source in ("even", "odd"):
        source_count = sum(
            transition_counts[(source, target)] for target in ("even", "odd")
        )
        for target in ("even", "odd"):
            count = transition_counts[(source, target)]
            features[f"{prefix}_{source}_to_{target}_rate"] = (
                count / transition_total
            )
            if target == "even":
                features[f"{prefix}_{source}_to_even_conditional_rate"] = (
                    count / source_count if source_count else 0.0
                )

    # Conditional next-digit distribution after the latest observed digit.
    # These are historical transitions wholly contained in the past window.
    latest_digit = window[-1]
    if include_latest_digit_transitions:
        latest_source_count = sum(
            digit_transition_counts[(latest_digit, target)] for target in range(10)
        )
        for target in range(10):
            count = digit_transition_counts[(latest_digit, target)]
            features[f"{prefix}_latest_digit_next_{target}_rate"] = (
                count / latest_source_count if latest_source_count else 0.0
            )

    # Current parity and exact-digit runs expose the latest sequence state.
    latest_parity = latest_digit % 2
    parity_streak = 0
    digit_streak = 0
    for digit in reversed(window):
        if digit % 2 != latest_parity:
            break
        parity_streak += 1
    for digit in reversed(window):
        if digit != latest_digit:
            break
        digit_streak += 1
    features[f"{prefix}_current_streak_length"] = float(parity_streak)
    features[f"{prefix}_current_digit_streak_length"] = float(digit_streak)
    features[f"{prefix}_latest_parity_even"] = float(latest_parity == 0)
    if include_latest_digit_identity:
        for digit in range(10):
            features[f"{prefix}_latest_digit_{digit}"] = float(latest_digit == digit)

    # Compare the most recent half-window with the preceding half-window.
    midpoint = total // 2
    previous_half = window[:midpoint]
    recent_half = window[midpoint:]
    previous_even_rate = _rate(previous_half, lambda digit: digit % 2 == 0)
    recent_even_rate = _rate(recent_half, lambda digit: digit % 2 == 0)
    features[f"{prefix}_even_rate_momentum"] = (
        recent_even_rate - previous_even_rate
    )
    # Individual digit momentum uses the same chronological half-window split.
    for digit in range(10):
        previous_rate = _rate(previous_half, lambda value, d=digit: value == d)
        recent_rate = _rate(recent_half, lambda value, d=digit: value == d)
        features[f"{prefix}_digit_{digit}_rate_momentum"] = (
            recent_rate - previous_rate
        )

    # Four chronological segments provide a coarse acceleration estimate:
    # change in the latest segment pair minus change in the earlier pair.
    # For tiny custom windows, acceleration is neutral rather than fabricated.
    if total >= 4:
        segments = [
            window[(index * total) // 4:((index + 1) * total) // 4]
            for index in range(4)
        ]
        even_segment_rates = [
            _rate(segment, lambda digit: digit % 2 == 0)
            for segment in segments
        ]
        features[f"{prefix}_even_rate_acceleration"] = (
            (even_segment_rates[3] - even_segment_rates[2])
            - (even_segment_rates[1] - even_segment_rates[0])
        )
    else:
        features[f"{prefix}_even_rate_acceleration"] = 0.0

    # Frequency of the exact parity suffix currently ending the window.
    # Overlapping occurrences are counted; all are historical, not future.
    if include_sequence_features:
        parity_values = tuple(digit % 2 for digit in window)
        for length in PARITY_SEQUENCE_LENGTHS:
            if total < length:
                features[f"{prefix}_latest_parity_sequence_{length}_rate"] = 0.0
                continue
            suffix = parity_values[-length:]
            occurrences = sum(
                1
                for index in range(total - length + 1)
                if parity_values[index:index + length] == suffix
            )
            features[f"{prefix}_latest_parity_sequence_{length}_rate"] = (
                occurrences / (total - length + 1)
            )

    return features


def _add_cross_window_features(
    features: dict[str, float],
    windows: Sequence[int],
) -> None:
    """Add short-versus-long frequency divergence using past-only features."""
    ordered_windows = sorted(windows)
    for short_window, long_window in zip(ordered_windows, ordered_windows[1:]):
        short_prefix = f"w{short_window}"
        long_prefix = f"w{long_window}"
        cross_prefix = f"cross_w{short_window}_w{long_window}"

        # Odd rate is the complement of even rate, and momentum deltas are
        # derivable from the window-level inputs. Keep only independent rate
        # divergences to avoid inflating the logistic feature matrix.
        base_keys = ["even_rate"]
        base_keys.extend(f"digit_{digit}_rate" for digit in range(10))
        for key in base_keys:
            features[f"{cross_prefix}_{key}_delta"] = (
                features[f"{short_prefix}_{key}"]
                - features[f"{long_prefix}_{key}"]
            )


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
    _validate_windows(windows)

    minimum_history = max(windows)
    if len(digits) <= minimum_history:
        return []

    rows = []
    for target_index in range(minimum_history, len(digits)):
        history = digits[:target_index]
        features: dict[str, float] = {}
        for size in windows:
            is_largest_window = size == minimum_history
            features.update(
                _window_features(
                    history[-size:],
                    f"w{size}",
                    include_latest_digit_identity=is_largest_window,
                    include_latest_digit_transitions=is_largest_window,
                    include_sequence_features=is_largest_window,
                )
            )
        _add_cross_window_features(features, windows)

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


def build_next_tick_features(
    digits: Sequence[int],
    windows: Sequence[int] = DEFAULT_WINDOWS,
) -> dict[str, float]:
    """Build pre-target features for the next unseen tick without inventing a label."""
    _validate_digits(digits)
    _validate_windows(windows)
    if len(digits) < max(windows):
        raise ValueError("Not enough historical digits for the requested feature windows.")

    # The appended placeholder is used only to emit the final feature row.
    # build_supervised_rows never includes the target digit in its features.
    prediction_rows = build_supervised_rows([*digits, 0], windows=windows)
    if not prediction_rows:
        raise ValueError("Unable to build next-tick prediction features.")
    return prediction_rows[-1]["features"]
