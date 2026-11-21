"""Adaptive, leakage-aware conditional pattern discovery for Even/Odd research.

This is a descriptive pattern miner, not a next-tick classifier. It searches
multiple data-driven context lengths and pattern families, then estimates
future-horizon outcome distributions from prior matching contexts only.
"""
from __future__ import annotations

from collections import Counter
from math import sqrt
from typing import Sequence


def _wilson(successes: int, total: int) -> dict:
    if total <= 0:
        return {"lower": None, "upper": None}
    z = 1.959963984540054
    rate = successes / total
    denominator = 1.0 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * sqrt((rate * (1.0 - rate) / total) + z * z / (4 * total * total)) / denominator
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin)}


def _adaptive_lengths(sample_size: int) -> list[int]:
    """Geometric candidate windows bounded by available sample size."""
    maximum = min(1000, sample_size // 4)
    if maximum < 8:
        return []
    values = []
    length = 8
    while length < maximum:
        values.append(length)
        length *= 2
    values.append(maximum)
    return sorted(set(values))


def _streak_length(values: Sequence[int]) -> int:
    if not values:
        return 0
    parity = values[-1] % 2
    count = 0
    for value in reversed(values):
        if value % 2 != parity:
            break
        count += 1
    return count


def _bucket_rate(rate: float) -> int:
    return min(9, max(0, int(rate * 10)))


def _bucket_momentum(momentum: float) -> int:
    return max(-10, min(10, int(round(momentum / 0.05))))


def _parity_regime(values: Sequence[int]) -> tuple[int, int, int, int]:
    half = len(values) // 2
    prior = values[:half]
    recent = values[half:]
    even_rate = sum(value % 2 == 0 for value in values) / len(values)
    prior_rate = sum(value % 2 == 0 for value in prior) / len(prior)
    recent_rate = sum(value % 2 == 0 for value in recent) / len(recent)
    streak = _streak_length(values)
    streak_bucket = min(8, streak)  # 8 means 8 or more
    return (
        _bucket_rate(even_rate),
        _bucket_momentum(recent_rate - prior_rate),
        streak_bucket,
        values[-1] % 2,
    )


def _digit_regime(values: Sequence[int], digit: int) -> tuple[int, int, int]:
    half = len(values) // 2
    prior = values[:half]
    recent = values[half:]
    rate = sum(value == digit for value in values) / len(values)
    prior_rate = sum(value == digit for value in prior) / len(prior)
    recent_rate = sum(value == digit for value in recent) / len(recent)
    return (
        _bucket_rate(rate),
        _bucket_momentum(recent_rate - prior_rate),
        values[-1] % 2,
    )


def _future_summary(digits: Sequence[int], end_index: int, horizon: int) -> dict:
    future = digits[end_index + 1:end_index + horizon + 1]
    odd_count = sum(value % 2 for value in future)
    even_count = horizon - odd_count
    longest_odd = 0
    longest_even = 0
    current_odd = 0
    current_even = 0
    digit_counts = [0] * 10
    for value in future:
        digit_counts[value] += 1
        if value % 2:
            current_odd += 1
            current_even = 0
        else:
            current_even += 1
            current_odd = 0
        longest_odd = max(longest_odd, current_odd)
        longest_even = max(longest_even, current_even)
    return {
        "odd_count": odd_count,
        "even_count": even_count,
        "three_or_more_odd": odd_count >= 3,
        "three_or_more_even": even_count >= 3,
        "three_consecutive_odd": longest_odd >= 3,
        "three_consecutive_even": longest_even >= 3,
        "digit_counts": digit_counts,
    }


def _summarize_matches(
    digits: Sequence[int],
    matches: Sequence[int],
    max_horizon: int,
    baseline: dict[int, list[dict]],
) -> list[dict]:
    results = []
    for horizon in range(1, max_horizon + 1):
        outcomes = [_future_summary(digits, index, horizon) for index in matches]
        total = len(outcomes)
        if not total:
            results.append({"horizon_ticks": horizon, "sample_size": 0})
            continue

        def event(key: str) -> dict:
            successes = sum(bool(item[key]) for item in outcomes)
            interval = _wilson(successes, total)
            return {
                "probability": successes / total,
                "successes": successes,
                "sample_size": total,
                "confidence_95": interval,
            }

        odd_counts = Counter(item["odd_count"] for item in outcomes)
        base = baseline[horizon]
        base_total = len(base)
        base_three_odd = sum(item["three_or_more_odd"] for item in base) / base_total
        base_three_consecutive_odd = sum(item["three_consecutive_odd"] for item in base) / base_total
        odd_count_distribution = [
            {"odd_count": count, "probability": odd_counts[count] / total}
            for count in range(horizon + 1)
        ]
        digit_occurrence = [
            {
                "digit": digit,
                "probability": sum(item["digit_counts"][digit] > 0 for item in outcomes) / total,
                "mean_occurrences": sum(item["digit_counts"][digit] for item in outcomes) / total,
            }
            for digit in range(10)
        ]
        p_three_odd = event("three_or_more_odd")
        p_three_consecutive_odd = event("three_consecutive_odd")
        results.append({
            "horizon_ticks": horizon,
            "sample_size": total,
            "mean_odd_count": sum(item["odd_count"] for item in outcomes) / total,
            "odd_count_distribution": odd_count_distribution,
            "probability_at_least_3_odd": p_three_odd,
            "probability_at_least_3_even": event("three_or_more_even"),
            "probability_3_consecutive_odd": p_three_consecutive_odd,
            "probability_3_consecutive_even": event("three_consecutive_even"),
            "digit_occurrence_probabilities": digit_occurrence,
            "unconditional_probability_at_least_3_odd": base_three_odd,
            "unconditional_probability_3_consecutive_odd": base_three_consecutive_odd,
            "lift_at_least_3_odd": p_three_odd["probability"] - base_three_odd,
            "lift_3_consecutive_odd": p_three_consecutive_odd["probability"] - base_three_consecutive_odd,
        })
    return results


def discover_patterns(digits: Sequence[int]) -> dict:
    """Discover currently active parity, digit, frequency and momentum patterns.

    Context lengths and forecast horizons are selected from the amount of
    available history. Future horizons are probability distributions over
    occurrence counts and streak events, not a single next-tick class label.
    All historical matches end before the latest complete outcome horizon.
    """
    if any(type(value) is not int or not 0 <= value <= 9 for value in digits):
        raise ValueError("Every last digit must be an integer from 0 to 9.")
    values = list(digits)
    count = len(values)
    if count < 100:
        return {
            "status": "insufficient_data",
            "observations": count,
            "minimum_observations": 100,
            "patterns": [],
            "warning": "At least 100 ordered live observations are required for exploratory pattern discovery.",
        }

    max_horizon = min(20, max(3, count // 25))
    max_suffix = min(20, max(2, count // 20))
    max_digit_suffix = min(4, max(1, count // 100))
    candidate_windows = _adaptive_lengths(count)
    last_complete_end = count - max_horizon - 1

    baseline_matches = list(range(0, last_complete_end + 1))
    baseline = {
        horizon: [_future_summary(values, index, horizon) for index in baseline_matches]
        for horizon in range(1, max_horizon + 1)
    }

    candidates: list[dict] = []
    parity = [value % 2 for value in values]

    # Exact parity suffixes: every available candidate length, not just 2–5.
    for length in range(2, max_suffix + 1):
        current = tuple(parity[-length:])
        matches = [
            end for end in range(length - 1, last_complete_end + 1)
            if tuple(parity[end - length + 1:end + 1]) == current
        ]
        candidates.append({
            "family": "parity_sequence",
            "context_ticks": length,
            "pattern": "".join("E" if value == 0 else "O" for value in current),
            "description": f"Exact parity suffix across {length} consecutive observations",
            "matches": matches,
        })

    # Exact digit suffixes discover recurring multi-digit sequences.
    for length in range(1, max_digit_suffix + 1):
        current = tuple(values[-length:])
        matches = [
            end for end in range(length - 1, last_complete_end + 1)
            if tuple(values[end - length + 1:end + 1]) == current
        ]
        candidates.append({
            "family": "digit_sequence",
            "context_ticks": length,
            "pattern": " ".join(str(value) for value in current),
            "description": f"Exact last-digit suffix across {length} observations",
            "matches": matches,
        })

    # Adaptive rolling windows: parity rate, momentum and current streak.
    for window in candidate_windows:
        current_signature = _parity_regime(values[-window:])
        matches = []
        for end in range(window - 1, last_complete_end + 1):
            if _parity_regime(values[end - window + 1:end + 1]) == current_signature:
                matches.append(end)
        candidates.append({
            "family": "parity_frequency_momentum",
            "context_ticks": window,
            "pattern": {
                "even_rate_bucket_10pp": current_signature[0],
                "even_momentum_bucket_5pp": current_signature[1],
                "current_streak_bucket": current_signature[2],
                "latest_parity": "even" if current_signature[3] == 0 else "odd",
            },
            "description": f"Parity frequency, momentum and streak regime over {window} observations",
            "matches": matches,
        })

        # Individual digit rate and momentum regimes for every digit 0–9.
        for digit in range(10):
            signature = _digit_regime(values[-window:], digit)
            digit_matches = [
                end for end in range(window - 1, last_complete_end + 1)
                if _digit_regime(values[end - window + 1:end + 1], digit) == signature
            ]
            candidates.append({
                "family": "individual_digit_frequency_momentum",
                "context_ticks": window,
                "pattern": {
                    "digit": digit,
                    "frequency_bucket_10pp": signature[0],
                    "momentum_bucket_5pp": signature[1],
                    "latest_parity": "even" if signature[2] == 0 else "odd",
                },
                "description": f"Digit {digit} frequency and momentum regime over {window} observations",
                "matches": digit_matches,
            })

    patterns = []
    for candidate in candidates:
        matches = candidate["matches"]
        if not matches:
            continue
        support = len(matches)
        if support < 5:
            support_status = "very_low"
        elif support < 20:
            support_status = "low"
        elif support < 50:
            support_status = "moderate"
        else:
            support_status = "strong"
        horizons = _summarize_matches(values, matches, max_horizon, baseline)
        largest_lift = max(
            (abs(item.get("lift_at_least_3_odd", 0.0)) for item in horizons if item.get("sample_size")),
            default=0.0,
        )
        patterns.append({
            "family": candidate["family"],
            "context_ticks": candidate["context_ticks"],
            "pattern": candidate["pattern"],
            "description": candidate["description"],
            "historical_matches": support,
            "support_status": support_status,
            "exploratory_lift_score": largest_lift * sqrt(support / (support + 50)),
            "horizons": horizons,
        })

    patterns.sort(
        key=lambda item: (
            item["exploratory_lift_score"],
            item["historical_matches"],
            item["context_ticks"],
        ),
        reverse=True,
    )
    return {
        "status": "exploratory",
        "observations": count,
        "adaptive_search": {
            "parity_sequence_lengths_tested": list(range(2, max_suffix + 1)),
            "digit_sequence_lengths_tested": list(range(1, max_digit_suffix + 1)),
            "frequency_windows_tested": candidate_windows,
            "future_horizons_tested": list(range(1, max_horizon + 1)),
            "selection_method": "Data-volume-adaptive geometric context windows plus all supported suffix lengths and future horizons",
        },
        "patterns_examined": len(candidates),
        "patterns_with_historical_matches": len(patterns),
        "patterns": patterns[:100],
        "latest_context": {
            "latest_digit": values[-1],
            "latest_parity": "even" if values[-1] % 2 == 0 else "odd",
            "current_parity_streak": _streak_length(values),
            "latest_parity_suffix": "".join("E" if value == 0 else "O" for value in parity[-max_suffix:]),
        },
        "interpretation": {
            "probabilities_are": "Empirical conditional frequencies over future horizons after prior matching contexts",
            "not_a_next_tick_classifier": True,
            "not_a_trading_signal": True,
            "support_threshold_for_evidence": 50,
            "warning": (
                "Exploratory conditional frequencies are vulnerable to sparse samples, "
                "overlapping observations and multiple-pattern selection. Confidence "
                "intervals quantify binomial sampling uncertainty only; they do not "
                "correct for dependence or multiple testing. No pattern is validated "
                "or authorized for trading by this report."
            ),
        },
    }
