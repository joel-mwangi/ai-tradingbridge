"""Chronological multi-model Even/Odd research with untouched holdout and walk-forward tests."""
from __future__ import annotations

from typing import Sequence

from app.even_odd.ml.evaluation import evaluate_even_probabilities
from app.even_odd.ml.features import DEFAULT_WINDOWS, build_supervised_rows
from app.even_odd.ml.model import (
    calibrate_model_probabilities,
    predict_even_probability,
    train_logistic_model,
    train_markov_transition_model,
)
from app.even_odd.ml.uncertainty import (
    bootstrap_log_loss_improvement,
    estimate_local_probability_interval,
    select_probability_interval,
)
from app.even_odd.ml.walk_forward import (
    chronological_research_split,
    expanding_walk_forward_splits,
)


def _fit_candidates(
    train_rows: Sequence[dict],
    validation_rows: Sequence[dict],
    calibration_rows: Sequence[dict],
    *,
    learning_rate: float,
    l2: float,
    max_epochs: int,
    patience: int,
) -> dict[str, dict]:
    """Fit independent candidate families using only the supplied past partitions."""
    candidates = {
        "logistic": train_logistic_model(
            train_rows, validation_rows,
            learning_rate=learning_rate, l2=l2,
            max_epochs=max_epochs, patience=patience,
        ),
        "markov": train_markov_transition_model(train_rows),
    }
    return {
        name: calibrate_model_probabilities(model, calibration_rows)
        for name, model in candidates.items()
    }


def run_research_experiment(
    digits: Sequence[int],
    *,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    learning_rate: float = 0.1,
    l2: float = 0.01,
    max_epochs: int = 200,
    patience: int = 20,
) -> dict:
    """Train and compare candidates using four disjoint chronological periods.

    The final test partition is never used to select a model or tune a threshold.
    Candidate status remains non-validated and execution-disabled.
    """
    rows = build_supervised_rows(digits, windows=windows)
    splits = chronological_research_split(rows)
    candidates = _fit_candidates(
        splits["train"], splits["validation"], splits["calibration"],
        learning_rate=learning_rate, l2=l2,
        max_epochs=max_epochs, patience=patience,
    )
    test_rows = splits["test"]
    actual = [row["target_parity"] for row in test_rows]
    candidate_metrics = {
        name: evaluate_even_probabilities(
            actual,
            [predict_even_probability(model, row["features"]) for row in test_rows],
        )
        for name, model in candidates.items()
    }
    constant_metrics = evaluate_even_probabilities(actual, [0.5] * len(test_rows))
    largest_window = max(windows)
    rolling_key = f"w{largest_window}_even_rate"
    if rolling_key not in test_rows[0]["features"]:
        raise ValueError("Largest rolling window feature is missing.")
    rolling_metrics = evaluate_even_probabilities(
        actual, [row["features"][rolling_key] for row in test_rows]
    )

    # Logistic remains the stable primary artifact for API compatibility. The
    # comparison report exposes both candidates; no candidate is promoted from
    # a single holdout result.
    return {
        "model": candidates["logistic"],
        "model_candidates": candidates,
        "candidate_metrics": candidate_metrics,
        "partitions": {name: len(partition) for name, partition in splits.items()},
        "test_target_index_range": {
            "start": test_rows[0]["target_index"],
            "end": test_rows[-1]["target_index"],
        },
        "test_metrics": candidate_metrics["logistic"],
        "constant_baseline_metrics": constant_metrics,
        "rolling_frequency_baseline_metrics": rolling_metrics,
        "promotion_status": {
            "validated": False,
            "calibrated": True,
            "execution_authorized": False,
            "reason": (
                "Candidate families are compared on an untouched chronological holdout. "
                "A single holdout is not sufficient for promotion; repeated walk-forward "
                "stability, calibrated uncertainty, payout-adjusted replay and independent "
                "approval are still required."
            ),
        },
    }


def run_walk_forward_evaluation(
    digits: Sequence[int],
    *,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    minimum_train_rows: int = 400,
    test_rows: int = 200,
    maximum_folds: int = 5,
    learning_rate: float = 0.1,
    l2: float = 0.01,
    max_epochs: int = 100,
    patience: int = 15,
    tick_epochs: Sequence[int] | None = None,
) -> dict:
    """Evaluate multiple candidate families on expanding, non-overlapping future blocks."""
    rows = build_supervised_rows(digits, windows=windows)
    if tick_epochs is not None:
        if len(tick_epochs) != len(digits) or any(type(epoch) is not int or epoch <= 0 for epoch in tick_epochs):
            raise ValueError("tick_epochs must contain one positive integer epoch per digit.")
        if list(tick_epochs) != sorted(tick_epochs):
            raise ValueError("tick_epochs must be chronological.")
        for row in rows:
            row["target_epoch"] = tick_epochs[row["target_index"]]
    folds = expanding_walk_forward_splits(
        rows, minimum_train_rows=minimum_train_rows,
        test_rows=test_rows, maximum_folds=maximum_folds,
    )
    all_actual: list[str] = []
    all_probabilities: dict[str, list[float]] = {"logistic": [], "markov": []}
    all_constant: list[float] = []
    all_rolling: list[float] = []
    oos_by_model: dict[str, list[dict]] = {"logistic": [], "markov": []}
    fold_reports = []
    rolling_key = f"w{max(windows)}_even_rate"

    for fold_number, fold in enumerate(folds, start=1):
        past = fold["train"]
        train_end = int(len(past) * 0.7)
        validation_end = int(len(past) * 0.85)
        train_partition = past[:train_end]
        validation_partition = past[train_end:validation_end]
        calibration_partition = past[validation_end:]
        candidates = _fit_candidates(
            train_partition, validation_partition, calibration_partition,
            learning_rate=learning_rate, l2=l2,
            max_epochs=max_epochs, patience=patience,
        )
        test_partition = fold["test"]
        actual = [row["target_parity"] for row in test_partition]
        probabilities = {
            name: [predict_even_probability(model, row["features"]) for row in test_partition]
            for name, model in candidates.items()
        }
        prior_intervals = {
            name: [
                estimate_local_probability_interval(
                    oos_by_model[name], target_probability=target_probability,
                )
                for target_probability in (0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9)
            ] if oos_by_model[name] else []
            for name in candidates
        }
        constant = [0.5] * len(test_partition)
        rolling = [row["features"][rolling_key] for row in test_partition]
        all_actual.extend(actual)
        all_constant.extend(constant)
        all_rolling.extend(rolling)
        for name in candidates:
            all_probabilities[name].extend(probabilities[name])
            new_predictions = []
            for row, probability in zip(test_partition, probabilities[name]):
                interval = (
                    select_probability_interval(
                        prior_intervals[name], target_probability=probability,
                    ) if prior_intervals[name] else {
                        "validated": False, "lower_probability": None,
                        "upper_probability": None,
                        "reason": "No prior out-of-sample calibration folds are available.",
                    }
                )
                prediction = {
                    "target_index": row["target_index"],
                    "fold": fold_number,
                    "probability_even": probability,
                    "constant_probability": 0.5,
                    "rolling_probability": row["features"][rolling_key],
                    "actual_parity": row["target_parity"],
                    "uncertainty_validated": interval.get("validated") is True,
                    "lower_even_probability": interval.get("lower_probability"),
                    "upper_even_probability": interval.get("upper_probability"),
                }
                if "target_epoch" in row:
                    prediction["target_epoch"] = row["target_epoch"]
                new_predictions.append(prediction)
            oos_by_model[name].extend(new_predictions)
        fold_reports.append({
            "fold": fold_number,
            "training_rows": len(train_partition),
            "validation_rows": len(validation_partition),
            "calibration_rows": len(calibration_partition),
            "test_rows": len(test_partition),
            "test_target_index_range": {
                "start": test_partition[0]["target_index"],
                "end": test_partition[-1]["target_index"],
            },
            "model_metrics": {
                name: evaluate_even_probabilities(actual, probabilities[name])
                for name in candidates
            },
            "constant_baseline_metrics": evaluate_even_probabilities(actual, constant),
            "rolling_frequency_baseline_metrics": evaluate_even_probabilities(actual, rolling),
        })

    intervals = {
        name: [
            estimate_local_probability_interval(
                predictions, target_probability=target_probability,
            )
            for target_probability in (0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9)
        ]
        for name, predictions in oos_by_model.items()
    }
    comparisons = {
        name: {
            "vs_constant_baseline": bootstrap_log_loss_improvement(
                predictions, baseline_key="constant_probability",
            ),
            "vs_rolling_frequency_baseline": bootstrap_log_loss_improvement(
                predictions, baseline_key="rolling_probability",
            ),
        }
        for name, predictions in oos_by_model.items()
    }
    return {
        "fold_count": len(fold_reports),
        "evaluated_rows": len(all_actual),
        "folds": fold_reports,
        "calibration_intervals_by_model": intervals,
        # Preserve the legacy key for logistic-regression API consumers.
        "calibration_intervals": intervals["logistic"],
        "statistical_comparisons_by_model": comparisons,
        "statistical_comparisons": comparisons["logistic"],
        "out_of_sample_predictions_by_model": oos_by_model,
        "out_of_sample_predictions": oos_by_model["logistic"],
        "aggregate_model_metrics_by_model": {
            name: evaluate_even_probabilities(all_actual, values)
            for name, values in all_probabilities.items()
        },
        "aggregate_model_metrics": evaluate_even_probabilities(
            all_actual, all_probabilities["logistic"]
        ),
        "aggregate_constant_baseline_metrics": evaluate_even_probabilities(
            all_actual, all_constant
        ),
        "aggregate_rolling_frequency_baseline_metrics": evaluate_even_probabilities(
            all_actual, all_rolling
        ),
        "promotion_status": {
            "validated": False,
            "execution_authorized": False,
            "reason": (
                "Multi-family walk-forward results are evidence for independent review, "
                "not automatic promotion. Stability across markets, calibrated uncertainty, "
                "historical payout replay and approval remain required."
            ),
        },
    }
