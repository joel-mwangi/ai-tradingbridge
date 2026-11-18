import random

import pytest

from app.even_odd.ml.uncertainty import (
    UncertaintyPolicy,
    bootstrap_log_loss_improvement,
    estimate_local_probability_interval,
    select_probability_interval,
)


def make_predictions():
    generator = random.Random(42)
    predictions = []
    for index in range(1500):
        probability = 0.70 + ((index % 5) - 2) * 0.005
        actual = "even" if generator.random() < 0.70 else "odd"
        predictions.append({
            "target_index": index,
            "fold": index // 300 + 1,
            "probability_even": probability,
            "actual_parity": actual,
        })
    return predictions


def test_block_bootstrap_interval_passes_sample_block_and_fold_stability_gates():
    result = estimate_local_probability_interval(
        make_predictions(),
        target_probability=0.70,
        policy=UncertaintyPolicy(
            minimum_local_observations=200,
            minimum_observations_per_fold=20,
            block_size=20,
            minimum_blocks=10,
            minimum_walk_forward_folds=5,
            maximum_fold_rate_spread=0.2,
            maximum_interval_width=0.25,
            bootstrap_replicates=300,
        ),
    )

    assert result["validated"] is True
    assert result["method"] == "walk_forward_non_overlapping_block_bootstrap_local_calibration"
    assert result["local_sample_size"] == 1500
    assert result["local_fold_count"] == 5
    assert result["local_block_count"] >= 10
    assert result["lower_probability"] <= result["point_estimate"]
    assert result["point_estimate"] <= result["upper_probability"]
    assert result["interval_width"] <= 0.25


def test_block_bootstrap_is_deterministic_for_same_seed():
    predictions = make_predictions()
    policy = UncertaintyPolicy(bootstrap_replicates=200)
    first = estimate_local_probability_interval(
        predictions,
        target_probability=0.70,
        policy=policy,
        seed=123,
    )
    second = estimate_local_probability_interval(
        predictions,
        target_probability=0.70,
        policy=policy,
        seed=123,
    )
    assert first == second


def test_uncertainty_remains_unvalidated_without_enough_walk_forward_folds():
    predictions = make_predictions()
    for item in predictions:
        item["fold"] = 1

    result = estimate_local_probability_interval(
        predictions,
        target_probability=0.70,
        policy=UncertaintyPolicy(bootstrap_replicates=200),
    )
    assert result["validated"] is False
    assert "folds" in result["reason"]


def test_uncertainty_returns_unvalidated_when_probability_band_is_sparse():
    result = estimate_local_probability_interval(
        make_predictions(),
        target_probability=0.05,
        policy=UncertaintyPolicy(bootstrap_replicates=200),
    )
    assert result["validated"] is False
    assert result["lower_probability"] is None


def test_uncertainty_rejects_duplicate_target_indices_and_bad_probabilities():
    predictions = make_predictions()
    predictions[1]["target_index"] = predictions[0]["target_index"]
    with pytest.raises(ValueError, match="unique"):
        estimate_local_probability_interval(
            predictions,
            target_probability=0.7,
        )

    with pytest.raises(ValueError, match="target_probability"):
        estimate_local_probability_interval(
            make_predictions(),
            target_probability=float("nan"),
        )



def test_select_probability_interval_requires_validated_nearby_band():
    intervals = [
        {
            "validated": True,
            "target_probability": 0.6,
            "probability_bandwidth": 0.1,
            "lower_probability": 0.52,
            "upper_probability": 0.68,
            "method": "block-bootstrap",
            "confidence_level": 0.9,
        },
        {
            "validated": False,
            "target_probability": 0.8,
            "probability_bandwidth": 0.1,
            "lower_probability": 0.7,
            "upper_probability": 0.9,
        },
    ]
    selected = select_probability_interval(intervals, target_probability=0.65)
    assert selected["validated"] is True
    assert selected["lower_probability"] == 0.52

    outside = select_probability_interval(intervals, target_probability=0.9)
    assert outside["validated"] is False
    assert outside["lower_probability"] is None


def test_select_probability_interval_refuses_when_no_validated_interval_exists():
    result = select_probability_interval(
        [{"validated": False, "target_probability": 0.5}],
        target_probability=0.5,
    )
    assert result["validated"] is False



def make_comparison_predictions(model_probability=0.8, baseline_probability=0.5):
    generator = random.Random(43)
    predictions = []
    for index in range(1500):
        actual = "even" if generator.random() < 0.70 else "odd"
        predictions.append({
            "target_index": index,
            "fold": index // 300 + 1,
            "probability_even": model_probability,
            "constant_probability": 0.5,
            "rolling_probability": baseline_probability,
            "actual_parity": actual,
        })
    return predictions


def test_block_bootstrap_supports_model_log_loss_improvement_over_baseline():
    result = bootstrap_log_loss_improvement(
        make_comparison_predictions(),
        baseline_key="constant_probability",
        bootstrap_replicates=300,
    )
    assert result["statistically_supported"] is True
    assert result["lower_improvement"] > 0
    assert result["positive_folds"] == 5
    assert result["approximate_one_sided_bootstrap_p"] < 0.05


def test_block_bootstrap_does_not_support_a_worse_model():
    result = bootstrap_log_loss_improvement(
        make_comparison_predictions(model_probability=0.2),
        baseline_key="constant_probability",
        bootstrap_replicates=300,
    )
    assert result["statistically_supported"] is False
    assert result["upper_improvement"] < 0


def test_block_bootstrap_reports_insufficient_sample_without_significance_claim():
    result = bootstrap_log_loss_improvement(
        make_comparison_predictions()[:100],
        baseline_key="constant_probability",
        bootstrap_replicates=200,
    )
    assert result["statistically_supported"] is False
    assert result["lower_improvement"] is None
