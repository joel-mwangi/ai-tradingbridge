"""End-to-end chronological Even/Odd model research experiment."""
from __future__ import annotations

from typing import Sequence

from app.even_odd.ml.evaluation import evaluate_even_probabilities
from app.even_odd.ml.features import DEFAULT_WINDOWS, build_supervised_rows
from app.even_odd.ml.model import (
    calibrate_model_probabilities,
    predict_even_probability,
    train_logistic_model,
)
from app.even_odd.ml.walk_forward import chronological_research_split


def run_research_experiment(
    digits: Sequence[int],
    *,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    learning_rate: float = 0.1,
    l2: float = 0.01,
    max_epochs: int = 200,
    patience: int = 20,
) -> dict:
    """Train, calibrate and evaluate using four disjoint chronological periods.

    The returned model is explicitly not marked validated or execution
    authorized. This experiment does not perform a profitability simulation.
    """
    rows = build_supervised_rows(digits, windows=windows)
    splits = chronological_research_split(rows)
    model = train_logistic_model(
        splits["train"],
        splits["validation"],
        learning_rate=learning_rate,
        l2=l2,
        max_epochs=max_epochs,
        patience=patience,
    )
    model = calibrate_model_probabilities(model, splits["calibration"])

    test_rows = splits["test"]
    actual = [row["target_parity"] for row in test_rows]
    model_probabilities = [
        predict_even_probability(model, row["features"])
        for row in test_rows
    ]
    constant_probabilities = [0.5] * len(test_rows)

    largest_window = max(windows)
    rolling_key = f"w{largest_window}_even_rate"
    if rolling_key not in test_rows[0]["features"]:
        raise ValueError("Largest rolling window feature is missing.")
    rolling_probabilities = [
        row["features"][rolling_key] for row in test_rows
    ]

    return {
        "model": model,
        "partitions": {
            name: len(partition) for name, partition in splits.items()
        },
        "test_target_index_range": {
            "start": test_rows[0]["target_index"],
            "end": test_rows[-1]["target_index"],
        },
        "test_metrics": evaluate_even_probabilities(actual, model_probabilities),
        "constant_baseline_metrics": evaluate_even_probabilities(
            actual, constant_probabilities
        ),
        "rolling_frequency_baseline_metrics": evaluate_even_probabilities(
            actual, rolling_probabilities
        ),
        "promotion_status": {
            "validated": False,
            "calibrated": True,
            "execution_authorized": False,
            "reason": (
                "A single chronological holdout experiment is not sufficient "
                "for promotion. Repeated walk-forward results, stability, "
                "payout-adjusted expected value and independent review are required."
            ),
        },
    }
