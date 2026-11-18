import pytest

from app.even_odd.ml.pipeline import run_research_experiment


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
