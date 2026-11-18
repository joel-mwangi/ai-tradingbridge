"""Small pure-Python logistic model for bounded Even/Odd research datasets.

This module is deliberately dependency-light for the serverless backend. A
trained model is not considered validated or execution-authorized merely
because optimization converged.
"""
from __future__ import annotations

from math import exp, isfinite, log
from typing import Sequence


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + exp(-value))
    exp_value = exp(value)
    return exp_value / (1.0 + exp_value)


def _log_loss(labels: Sequence[int], probabilities: Sequence[float]) -> float:
    epsilon = 1e-15
    return -sum(
        target * log(max(epsilon, probability))
        + (1 - target) * log(max(epsilon, 1 - probability))
        for target, probability in zip(labels, probabilities)
    ) / len(labels)


def _extract_matrix(
    rows: Sequence[dict],
    feature_names: Sequence[str] | None = None,
) -> tuple[list[list[float]], list[int], list[str]]:
    if not rows:
        raise ValueError("Training and validation rows cannot be empty.")
    if feature_names is None:
        features = rows[0].get("features")
        if not isinstance(features, dict) or not features:
            raise ValueError("Every row must contain a non-empty features mapping.")
        feature_names = sorted(features)

    matrix: list[list[float]] = []
    labels: list[int] = []
    expected = set(feature_names)
    for row in rows:
        features = row.get("features")
        label = row.get("target_parity")
        if not isinstance(features, dict) or set(features) != expected:
            raise ValueError("All rows must contain the same feature keys.")
        if label not in {"even", "odd"}:
            raise ValueError("Every target_parity must be even or odd.")
        vector = []
        for name in feature_names:
            value = features[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Model features must be numeric.")
            if not isfinite(value):
                raise ValueError("Model features must be finite.")
            vector.append(float(value))
        matrix.append(vector)
        labels.append(1 if label == "even" else 0)

    if len(set(labels)) < 2:
        raise ValueError("Training and validation data must each contain both parity classes.")
    return matrix, labels, list(feature_names)


def train_logistic_model(
    train_rows: Sequence[dict],
    validation_rows: Sequence[dict],
    *,
    learning_rate: float = 0.1,
    l2: float = 0.01,
    max_epochs: int = 200,
    patience: int = 20,
) -> dict:
    """Fit standardized logistic regression and early-stop on validation loss.

    The validation set is used for model selection and must not also be used
    as the final test set. Fit all preprocessing parameters on training rows
    only. The returned model remains uncalibrated and unvalidated.
    """
    if len(train_rows) < 20 or len(validation_rows) < 20:
        raise ValueError("At least 20 training and 20 validation rows are required.")
    if not isfinite(learning_rate) or not 0 < learning_rate <= 1:
        raise ValueError("learning_rate must be finite and in (0, 1].")
    if not isfinite(l2) or l2 < 0:
        raise ValueError("l2 must be finite and non-negative.")
    if type(max_epochs) is not int or not 1 <= max_epochs <= 1000:
        raise ValueError("max_epochs must be an integer from 1 to 1000.")
    if type(patience) is not int or not 1 <= patience <= max_epochs:
        raise ValueError("patience must be an integer from 1 to max_epochs.")

    train_x, train_y, feature_names = _extract_matrix(train_rows)
    validation_x, validation_y, _ = _extract_matrix(validation_rows, feature_names)

    feature_count = len(feature_names)
    row_count = len(train_x)
    means = [
        sum(row[index] for row in train_x) / row_count
        for index in range(feature_count)
    ]
    scales = []
    for index, mean in enumerate(means):
        variance = sum((row[index] - mean) ** 2 for row in train_x) / row_count
        standard_deviation = variance ** 0.5
        scales.append(standard_deviation if standard_deviation > 1e-12 else 1.0)

    train_z = [
        [(value - means[index]) / scales[index] for index, value in enumerate(row)]
        for row in train_x
    ]
    validation_z = [
        [(value - means[index]) / scales[index] for index, value in enumerate(row)]
        for row in validation_x
    ]

    weights = [0.0] * feature_count
    intercept = 0.0
    best_weights = list(weights)
    best_intercept = intercept
    best_loss = float("inf")
    best_epoch = 0
    epochs_without_improvement = 0

    for epoch in range(1, max_epochs + 1):
        gradient = [0.0] * feature_count
        intercept_gradient = 0.0
        for vector, target in zip(train_z, train_y):
            probability = _sigmoid(intercept + sum(
                weight * value for weight, value in zip(weights, vector)
            ))
            error = probability - target
            intercept_gradient += error
            for index, value in enumerate(vector):
                gradient[index] += error * value

        intercept -= learning_rate * intercept_gradient / row_count
        for index in range(feature_count):
            gradient_value = gradient[index] / row_count + l2 * weights[index]
            weights[index] -= learning_rate * gradient_value

        validation_probabilities = [
            _sigmoid(intercept + sum(
                weight * value for weight, value in zip(weights, vector)
            ))
            for vector in validation_z
        ]
        validation_loss = _log_loss(validation_y, validation_probabilities)
        if validation_loss < best_loss - 1e-10:
            best_loss = validation_loss
            best_weights = list(weights)
            best_intercept = intercept
            best_epoch = epoch
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= patience:
                break

    return {
        "model_type": "standardized_logistic_regression",
        "feature_names": feature_names,
        "means": means,
        "scales": scales,
        "weights": best_weights,
        "intercept": best_intercept,
        "best_epoch": best_epoch,
        "validation_log_loss": best_loss,
        "training_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "calibrated": False,
        "validated": False,
        "execution_authorized": False,
    }


def predict_even_probability(model: dict, features: dict[str, float]) -> float:
    """Return P(even) from a fitted model without changing its validation state."""
    required = model.get("feature_names")
    if not isinstance(required, list) or set(features) != set(required):
        raise ValueError("Prediction features do not match the trained model schema.")
    means = model["means"]
    scales = model["scales"]
    weights = model["weights"]
    if not (len(required) == len(means) == len(scales) == len(weights)):
        raise ValueError("Trained model parameter dimensions are inconsistent.")

    score = float(model["intercept"])
    for index, name in enumerate(required):
        value = features[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError("Prediction features must be finite numeric values.")
        if not isfinite(means[index]) or not isfinite(scales[index]) or scales[index] <= 0:
            raise ValueError("Trained model scaling parameters are invalid.")
        score += weights[index] * ((float(value) - means[index]) / scales[index])
    return _sigmoid(score)
