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
from app.even_odd.ml.walk_forward import (
    chronological_research_split,
    expanding_walk_forward_splits,
)


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



def run_walk_forward_evaluation(
    digits: Sequence[int],
    *,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    minimum_train_rows: int = 200,
    test_rows: int = 200,
    maximum_folds: int = 5,
    learning_rate: float = 0.1,
    l2: float = 0.01,
    max_epochs: int = 100,
    patience: int = 15,
) -> dict:
    """Evaluate expanding-window future blocks against constant and rolling baselines."""
    rows = build_supervised_rows(digits, windows=windows)
    folds = expanding_walk_forward_splits(
        rows,
        minimum_train_rows=minimum_train_rows,
        test_rows=test_rows,
        maximum_folds=maximum_folds,
    )
    all_actual: list[str] = []
    all_model_probabilities: list[float] = []
    all_constant_probabilities: list[float] = []
    all_rolling_probabilities: list[float] = []
    fold_reports = []
    rolling_key = f"w{max(windows)}_even_rate"

    for fold_number, fold in enumerate(folds, start=1):
        train_validation = fold["train"]
        train_end = int(len(train_validation) * 0.8)
        train_partition = train_validation[:train_end]
        validation_partition = train_validation[train_end:]
        model = train_logistic_model(
            train_partition,
            validation_partition,
            learning_rate=learning_rate,
            l2=l2,
            max_epochs=max_epochs,
            patience=patience,
        )
        test_partition = fold["test"]
        actual = [row["target_parity"] for row in test_partition]
        model_probabilities = [
            predict_even_probability(model, row["features"])
            for row in test_partition
        ]
        constant_probabilities = [0.5] * len(test_partition)
        rolling_probabilities = [
            row["features"][rolling_key] for row in test_partition
        ]

        all_actual.extend(actual)
        all_model_probabilities.extend(model_probabilities)
        all_constant_probabilities.extend(constant_probabilities)
        all_rolling_probabilities.extend(rolling_probabilities)
        fold_reports.append({
            "fold": fold_number,
            "training_rows": len(train_partition),
            "validation_rows": len(validation_partition),
            "test_rows": len(test_partition),
            "test_target_index_range": {
                "start": test_partition[0]["target_index"],
                "end": test_partition[-1]["target_index"],
            },
            "model_metrics": evaluate_even_probabilities(
                actual, model_probabilities
            ),
            "constant_baseline_metrics": evaluate_even_probabilities(
                actual, constant_probabilities
            ),
            "rolling_frequency_baseline_metrics": evaluate_even_probabilities(
                actual, rolling_probabilities
            ),
        })

    return {
        "fold_count": len(fold_reports),
        "evaluated_rows": len(all_actual),
        "folds": fold_reports,
        "aggregate_model_metrics": evaluate_even_probabilities(
            all_actual, all_model_probabilities
        ),
        "aggregate_constant_baseline_metrics": evaluate_even_probabilities(
            all_actual, all_constant_probabilities
        ),
        "aggregate_rolling_frequency_baseline_metrics": evaluate_even_probabilities(
            all_actual, all_rolling_probabilities
        ),
        "promotion_status": {
            "validated": False,
            "execution_authorized": False,
            "reason": (
                "Walk-forward metrics are evidence for review, not automatic "
                "promotion. Stability, calibration, multiple-market testing, "
                "payout-adjusted EV and independent approval remain required."
            ),
        },
    }
