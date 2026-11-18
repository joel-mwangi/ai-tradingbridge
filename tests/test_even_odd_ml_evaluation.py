import math

import pytest

from app.even_odd.ml.evaluation import evaluate_even_probabilities
from app.even_odd.ml.walk_forward import expanding_walk_forward_splits


def test_evaluation_reports_probability_and_calibration_metrics():
    actual = ["even", "odd", "even", "odd"]
    probabilities = [0.8, 0.2, 0.7, 0.3]
    result = evaluate_even_probabilities(actual, probabilities)

    assert result["sample_size"] == 4
    assert result["accuracy"] == 1.0
    assert result["balanced_accuracy"] == 1.0
    assert result["log_loss"] < math.log(2)
    assert result["brier_score"] < 0.25
    assert result["confusion_matrix"]["true_even_predicted_even"] == 2


def test_evaluation_exposes_random_baseline():
    result = evaluate_even_probabilities(["even", "odd"], [0.5, 0.5])
    assert result["log_loss"] == pytest.approx(math.log(2))
    assert result["brier_score"] == pytest.approx(0.25)
    assert result["baseline"]["even_probability"] == 0.5


@pytest.mark.parametrize(
    ("actual", "probabilities"),
    [
        ([], []),
        (["even"], [float("nan")]),
        (["odd"], [1.1]),
        (["unknown"], [0.5]),
        (["even"], [True]),
        (["even", "odd"], [0.5]),
    ],
)
def test_evaluation_rejects_invalid_inputs(actual, probabilities):
    with pytest.raises(ValueError):
        evaluate_even_probabilities(actual, probabilities)


def test_walk_forward_splits_are_expanding_and_non_overlapping():
    rows = [{"target_index": index} for index in range(30)]
    folds = expanding_walk_forward_splits(
        rows,
        minimum_train_rows=10,
        test_rows=5,
    )

    assert len(folds) == 4
    assert len(folds[0]["train"]) == 10
    assert [row["target_index"] for row in folds[0]["test"]] == list(range(10, 15))
    assert len(folds[1]["train"]) == 15
    assert [row["target_index"] for row in folds[1]["test"]] == list(range(15, 20))
    assert folds[-1]["test"][-1]["target_index"] == 29


def test_walk_forward_rejects_shuffled_rows_and_invalid_sizes():
    rows = [{"target_index": index} for index in range(20)]
    rows[2], rows[3] = rows[3], rows[2]
    with pytest.raises(ValueError, match="ordered"):
        expanding_walk_forward_splits(rows, minimum_train_rows=10, test_rows=5)
    with pytest.raises(ValueError, match="at least 5"):
        expanding_walk_forward_splits(rows, minimum_train_rows=4, test_rows=5)
