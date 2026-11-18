import pytest

from app.even_odd.ml.pipeline import (
    run_research_experiment,
    run_walk_forward_evaluation,
)


def test_research_pipeline_uses_disjoint_periods_and_keeps_execution_disabled():
    digits = [0, 1] * 500
    result = run_research_experiment(
        digits,
        windows=(20,),
        max_epochs=60,
        patience=10,
    )

    partitions = result["partitions"]
    assert partitions["train"] > 20
    assert partitions["validation"] >= 20
    assert partitions["calibration"] >= 50
    assert partitions["test"] > 0
    assert sum(partitions.values()) == 980
    assert result["test_metrics"]["sample_size"] == partitions["test"]
    assert result["constant_baseline_metrics"]["log_loss"] > 0
    assert result["rolling_frequency_baseline_metrics"]["sample_size"] == partitions["test"]
    assert result["model"]["calibrated"] is True
    assert result["model"]["validated"] is False
    assert result["model"]["execution_authorized"] is False
    assert result["promotion_status"]["validated"] is False


def test_research_pipeline_rejects_insufficient_history():
    with pytest.raises(ValueError):
        run_research_experiment([0, 1] * 10, windows=(10,))



def test_walk_forward_pipeline_evaluates_multiple_future_blocks():
    digits = [0, 1] * 400
    result = run_walk_forward_evaluation(
        digits,
        windows=(20,),
        minimum_train_rows=400,
        test_rows=100,
        maximum_folds=3,
        max_epochs=30,
        patience=8,
    )

    assert result["fold_count"] == 3
    assert result["evaluated_rows"] == 300
    assert [fold["fold"] for fold in result["folds"]] == [1, 2, 3]
    assert all(fold["training_rows"] >= 80 for fold in result["folds"])
    assert all(fold["validation_rows"] >= 20 for fold in result["folds"])
    assert all(fold["calibration_rows"] >= 50 for fold in result["folds"])
    assert result["aggregate_model_metrics"]["sample_size"] == 300
    assert len(result["out_of_sample_predictions"]) == 300
    assert len(result["calibration_intervals"]) == 9
    assert all(interval["validated"] is False for interval in result["calibration_intervals"])
    assert "vs_constant_baseline" in result["statistical_comparisons"]
    assert "vs_rolling_frequency_baseline" in result["statistical_comparisons"]
    assert result["promotion_status"]["validated"] is False


def test_walk_forward_pipeline_rejects_insufficient_rows():
    with pytest.raises(ValueError):
        run_walk_forward_evaluation(
            [0, 1] * 20,
            windows=(10,),
            minimum_train_rows=100,
            test_rows=50,
        )


def test_walk_forward_default_training_rows_provide_calibration_minimum():
    result = run_walk_forward_evaluation(
        [0, 1] * 500,
        windows=(20,),
        test_rows=50,
        maximum_folds=1,
        max_epochs=20,
        patience=5,
    )

    first_fold = result["folds"][0]
    assert first_fold["calibration_rows"] >= 50
    assert first_fold["validation_rows"] >= 20
