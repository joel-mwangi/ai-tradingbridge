"""Out-of-sample classification metrics for Even/Odd probability forecasts."""
from __future__ import annotations

from math import isfinite, log
from typing import Sequence


def evaluate_even_probabilities(
    actual_parity: Sequence[str],
    predicted_even_probability: Sequence[float],
    *,
    calibration_bins: int = 10,
) -> dict:
    """Evaluate probability forecasts; probability is P(next digit is even)."""
    if len(actual_parity) != len(predicted_even_probability) or not actual_parity:
        raise ValueError("Actual labels and probabilities must be non-empty and equal length.")
    if calibration_bins < 2 or calibration_bins > 50:
        raise ValueError("calibration_bins must be between 2 and 50.")
    if any(label not in {"even", "odd"} for label in actual_parity):
        raise ValueError("Actual parity labels must be even or odd.")

    probabilities = []
    for probability in predicted_even_probability:
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("Predicted probabilities must be numeric.")
        if not isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Predicted probabilities must be finite and between 0 and 1.")
        probabilities.append(float(probability))

    count = len(actual_parity)
    targets = [1 if label == "even" else 0 for label in actual_parity]
    epsilon = 1e-15
    log_loss = -sum(
        target * log(max(epsilon, probability))
        + (1 - target) * log(max(epsilon, 1 - probability))
        for target, probability in zip(targets, probabilities)
    ) / count
    brier_score = sum(
        (probability - target) ** 2
        for target, probability in zip(targets, probabilities)
    ) / count

    predicted = [probability >= 0.5 for probability in probabilities]
    true_positive = sum(target == 1 and prediction for target, prediction in zip(targets, predicted))
    true_negative = sum(target == 0 and not prediction for target, prediction in zip(targets, predicted))
    false_positive = sum(target == 0 and prediction for target, prediction in zip(targets, predicted))
    false_negative = sum(target == 1 and not prediction for target, prediction in zip(targets, predicted))
    accuracy = (true_positive + true_negative) / count
    even_recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    odd_recall = true_negative / (true_negative + false_positive) if true_negative + false_positive else 0.0

    bins = []
    expected_calibration_error = 0.0
    for index in range(calibration_bins):
        lower = index / calibration_bins
        upper = (index + 1) / calibration_bins
        selected = [
            (target, probability)
            for target, probability in zip(targets, probabilities)
            if lower <= probability < upper
            or (index == calibration_bins - 1 and probability == 1.0)
        ]
        if not selected:
            continue
        observed_rate = sum(target for target, _ in selected) / len(selected)
        mean_probability = sum(probability for _, probability in selected) / len(selected)
        weight = len(selected) / count
        expected_calibration_error += weight * abs(observed_rate - mean_probability)
        bins.append({
            "lower_bound": lower,
            "upper_bound": upper,
            "count": len(selected),
            "mean_predicted_even_probability": mean_probability,
            "observed_even_rate": observed_rate,
        })

    return {
        "sample_size": count,
        "accuracy": accuracy,
        "balanced_accuracy": (even_recall + odd_recall) / 2,
        "log_loss": log_loss,
        "brier_score": brier_score,
        "expected_calibration_error": expected_calibration_error,
        "confusion_matrix": {
            "true_even_predicted_even": true_positive,
            "true_even_predicted_odd": false_negative,
            "true_odd_predicted_even": false_positive,
            "true_odd_predicted_odd": true_negative,
        },
        "calibration_bins": bins,
        "baseline": {
            "even_probability": 0.5,
            "log_loss": log(2),
            "brier_score": 0.25,
        },
        "warning": (
            "Metrics describe this evaluation sample only. A model must beat "
            "simple baselines across repeated chronological out-of-sample folds "
            "and remain calibrated before its probabilities can inform trading."
        ),
    }
