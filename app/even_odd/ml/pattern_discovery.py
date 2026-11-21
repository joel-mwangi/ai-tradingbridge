"""Adaptive, leakage-aware conditional pattern discovery for Even/Odd research.

This is a descriptive pattern miner, not a next-tick classifier. It searches
multiple data-driven context lengths and pattern families, estimates outcome
distributions over future horizons, and checks candidate horizons on a later
chronological slice of historical pattern occurrences.
"""
from __future__ import annotations

from collections import Counter
from math import sqrt
from typing import Sequence


EVENT_KEYS = (
    "three_or_more_odd",
    "three_or_more_even",
    "three_consecutive_odd",
    "three_consecutive_even",
)


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


def _build_prefixes(values: Sequence[int]) -> tuple[list[int], list[list[int]], list[int]]:
    count = len(values)
    even_prefix = [0] * (count + 1)
    digit_prefix = [[0] * (count + 1) for _ in range(10)]
    streak_lengths = [0] * count
    for index, value in enumerate(values):
        even_prefix[index + 1] = even_prefix[index] + int(value % 2 == 0)
        for digit in range(10):
            digit_prefix[digit][index + 1] = digit_prefix[digit][index] + int(value == digit)
        streak_lengths[index] = (
            streak_lengths[index - 1] + 1
            if index and values[index - 1] % 2 == value % 2
            else 1
        )
    return even_prefix, digit_prefix, streak_lengths


def _parity_signature_at(
    values: Sequence[int],
    even_prefix: Sequence[int],
    streak_lengths: Sequence[int],
    end: int,
    window: int,
) -> tuple[int, int, int, int]:
    start = end - window + 1
    half_start = start + window // 2
    prior_size = half_start - start
    recent_size = end + 1 - half_start
    even_rate = (even_prefix[end + 1] - even_prefix[start]) / window
    prior_rate = (even_prefix[half_start] - even_prefix[start]) / prior_size
    recent_rate = (even_prefix[end + 1] - even_prefix[half_start]) / recent_size
    return (
        _bucket_rate(even_rate),
        _bucket_momentum(recent_rate - prior_rate),
        min(8, streak_lengths[end]),
        values[end] % 2,
    )


def _digit_signature_at(
    values: Sequence[int],
    digit_prefix: Sequence[int],
    end: int,
    window: int,
    digit: int,
) -> tuple[int, int, int]:
    start = end - window + 1
    half_start = start + window // 2
    prior_size = half_start - start
    recent_size = end + 1 - half_start
    counts = digit_prefix
    rate = (counts[end + 1] - counts[start]) / window
    prior_rate = (counts[half_start] - counts[start]) / prior_size
    recent_rate = (counts[end + 1] - counts[half_start]) / recent_size
    return (
        _bucket_rate(rate),
        _bucket_momentum(recent_rate - prior_rate),
        values[end] % 2,
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
            return {
                "probability": successes / total,
                "successes": successes,
                "sample_size": total,
                "confidence_95": _wilson(successes, total),
            }

        odd_counts = Counter(item["odd_count"] for item in outcomes)
        base = baseline[horizon]
        base_total = len(base)
        base_three_odd = sum(item["three_or_more_odd"] for item in base) / base_total
        base_three_consecutive_odd = sum(item["three_consecutive_odd"] for item in base) / base_total
        odd_count_distribution = [
            {"odd_count": value, "probability": odd_counts[value] / total}
            for value in range(horizon + 1)
        ]
        digit_detail_horizon = min(5, max_horizon)
        digit_occurrence = [
            {
                "digit": digit,
                "probability": sum(item["digit_counts"][digit] > 0 for item in outcomes) / total,
                "mean_occurrences": sum(item["digit_counts"][digit] for item in outcomes) / total,
            }
            for digit in range(10)
        ] if horizon == digit_detail_horizon else []
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


def _holdout_horizon_selection(
    digits: Sequence[int],
    matches: Sequence[int],
    max_horizon: int,
    last_complete_end: int,
) -> dict:
    """Select candidate horizons using later pattern occurrences only."""
    split = int(len(matches) * 0.7)
    train_matches = list(matches[:split])
    test_matches = list(matches[split:])
    if len(train_matches) < 10 or len(test_matches) < 10:
        return {
            "status": "insufficient_occurrences",
            "training_matches": len(train_matches),
            "holdout_matches": len(test_matches),
            "selected_horizon_by_event": {},
            "warning": "At least 10 earlier and 10 later matching occurrences are required for horizon comparison.",
        }

    baseline_end = min(train_matches[-1], last_complete_end)
    baseline_indices = range(0, baseline_end + 1)
    selected = {}
    comparisons = {key: [] for key in EVENT_KEYS}

    for horizon in range(3, max_horizon + 1):
        train_outcomes = [_future_summary(digits, index, horizon) for index in train_matches]
        test_outcomes = [_future_summary(digits, index, horizon) for index in test_matches]
        baseline_outcomes = [_future_summary(digits, index, horizon) for index in baseline_indices]
        if not baseline_outcomes:
            continue
        for event_key in EVENT_KEYS:
            pattern_probability = sum(item[event_key] for item in train_outcomes) / len(train_outcomes)
            baseline_probability = sum(item[event_key] for item in baseline_outcomes) / len(baseline_outcomes)
            model_brier = sum((pattern_probability - int(item[event_key])) ** 2 for item in test_outcomes) / len(test_outcomes)
            baseline_brier = sum((baseline_probability - int(item[event_key])) ** 2 for item in test_outcomes) / len(test_outcomes)
            comparisons[event_key].append({
                "horizon_ticks": horizon,
                "training_probability": pattern_probability,
                "baseline_probability": baseline_probability,
                "holdout_brier_score": model_brier,
                "baseline_holdout_brier_score": baseline_brier,
                "brier_improvement": baseline_brier - model_brier,
                "training_matches": len(train_matches),
                "holdout_matches": len(test_matches),
            })

    for event_key, items in comparisons.items():
        if not items:
            continue
        best = max(items, key=lambda item: item["brier_improvement"])
        if best["brier_improvement"] > 0:
            selected[event_key] = best

    return {
        "status": "positive_holdout_brier_improvement" if selected else "no_positive_holdout_brier_improvement",
        "training_matches": len(train_matches),
        "holdout_matches": len(test_matches),
        "selected_horizon_by_event": selected,
        "candidate_horizons_evaluated": list(range(3, max_horizon + 1)),
        "selection_method": "Chronological 70/30 split of matching occurrences; compare each event's holdout Brier score with an unconditional-rate baseline.",
        "warning": (
            "Horizon selection is exploratory. Searching multiple patterns and horizons "
            "can create false discoveries; no multiple-testing correction or independent "
            "approval is implied."
        ),
    }


def discover_patterns(digits: Sequence[int]) -> dict:
    """Discover currently active parity, digit, frequency and momentum patterns.

    Context lengths and future horizons adapt to the available history. The
    report estimates future event distributions after prior matching contexts
    and uses a later chronological slice to compare candidate horizons.
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

    max_horizon = min(50, max(3, int(sqrt(count))))
    max_suffix = min(20, max(2, count // 20))
    max_digit_suffix = min(20, max(1, count // 20))
    candidate_windows = _adaptive_lengths(count)
    last_complete_end = count - max_horizon - 1
    even_prefix, digit_prefixes, streak_lengths = _build_prefixes(values)

    baseline_matches = list(range(0, last_complete_end + 1))
    baseline = {
        horizon: [_future_summary(values, index, horizon) for index in baseline_matches]
        for horizon in range(1, max_horizon + 1)
    }

    candidates: list[dict] = []
    parity = [value % 2 for value in values]

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

    for window in candidate_windows:
        current_parity_signature = _parity_signature_at(
            values, even_prefix, streak_lengths, count - 1, window,
        )
        parity_matches = [
            end for end in range(window - 1, last_complete_end + 1)
            if _parity_signature_at(values, even_prefix, streak_lengths, end, window) == current_parity_signature
        ]
        candidates.append({
            "family": "parity_frequency_momentum",
            "context_ticks": window,
            "pattern": {
                "even_rate_bucket_10pp": current_parity_signature[0],
                "even_momentum_bucket_5pp": current_parity_signature[1],
                "current_streak_bucket": current_parity_signature[2],
                "latest_parity": "even" if current_parity_signature[3] == 0 else "odd",
            },
            "description": f"Parity frequency, momentum and streak regime over {window} observations",
            "matches": parity_matches,
        })

        for digit in range(10):
            current_digit_signature = _digit_signature_at(
                values, digit_prefixes[digit], count - 1, window, digit,
            )
            digit_matches = [
                end for end in range(window - 1, last_complete_end + 1)
                if _digit_signature_at(values, digit_prefixes[digit], end, window, digit) == current_digit_signature
            ]
            candidates.append({
                "family": "individual_digit_frequency_momentum",
                "context_ticks": window,
                "pattern": {
                    "digit": digit,
                    "frequency_bucket_10pp": current_digit_signature[0],
                    "momentum_bucket_5pp": current_digit_signature[1],
                    "latest_parity": "even" if current_digit_signature[2] == 0 else "odd",
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
        holdout = _holdout_horizon_selection(values, matches, max_horizon, last_complete_end)
        best_holdout_improvement = max(
            (
                item["brier_improvement"]
                for item in holdout.get("selected_horizon_by_event", {}).values()
            ),
            default=None,
        )
        patterns.append({
            "family": candidate["family"],
            "context_ticks": candidate["context_ticks"],
            "pattern": candidate["pattern"],
            "description": candidate["description"],
            "historical_matches": support,
            "support_status": support_status,
            "exploratory_lift_score": largest_lift * sqrt(support / (support + 50)),
            "holdout_horizon_selection": holdout,
            "best_holdout_brier_improvement": best_holdout_improvement,
            "horizons": horizons,
        })

    patterns.sort(
        key=lambda item: (
            item["best_holdout_brier_improvement"] is not None,
            item["best_holdout_brier_improvement"] or 0.0,
            item["exploratory_lift_score"],
            item["historical_matches"],
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
            "selection_method": "Data-volume-adaptive context lengths and future horizons; candidate horizons compared on later matching occurrences",
        },
        "patterns_examined": len(candidates),
        "patterns_with_historical_matches": len(patterns),
        "patterns": patterns[:50],
        "latest_context": {
            "latest_digit": values[-1],
            "latest_parity": "even" if values[-1] % 2 == 0 else "odd",
            "current_parity_streak": streak_lengths[-1],
            "latest_parity_suffix": "".join("E" if value == 0 else "O" for value in parity[-max_suffix:]),
        },
        "interpretation": {
            "probabilities_are": "Empirical conditional frequencies over future horizons after prior matching contexts",
            "not_a_next_tick_classifier": True,
            "not_a_trading_signal": True,
            "support_threshold_for_evidence": 50,
            "warning": (
                "Exploratory conditional frequencies are vulnerable to sparse samples, "
                "overlapping observations and multiple-pattern selection. Wilson intervals "
                "quantify binomial sampling uncertainty only; they do not correct for "
                "dependence or multiple testing. Horizon selection is exploratory and "
                "does not validate a pattern or authorize trading."
            ),
        },
    }
