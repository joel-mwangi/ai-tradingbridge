"""Block-bootstrap uncertainty for local calibration of walk-forward forecasts.

Intervals estimate the observed even rate among out-of-sample forecasts near a
requested probability. They are not distribution-free guarantees: validation
also requires enough contiguous blocks and stable rates across walk-forward
folds. The output remains explicitly unvalidated when those gates fail.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from math import ceil, floor, isfinite, log, log
from typing import Sequence


@dataclass(frozen=True, slots=True)
class UncertaintyPolicy:
    confidence_level: float = 0.90
    probability_bandwidth: float = 0.10
    minimum_local_observations: int = 200
    minimum_observations_per_fold: int = 20
    block_size: int = 20
    minimum_blocks: int = 10
    minimum_walk_forward_folds: int = 5
    maximum_fold_rate_spread: float = 0.15
    maximum_interval_width: float = 0.25
    bootstrap_replicates: int = 500

    def __post_init__(self) -> None:
        if (
            isinstance(self.confidence_level, bool)
            or not isinstance(self.confidence_level, (int, float))
            or not isfinite(self.confidence_level)
            or not 0.5 < self.confidence_level < 1
        ):
            raise ValueError("confidence_level must be in (0.5, 1).")
        if (
            isinstance(self.probability_bandwidth, bool)
            or not isinstance(self.probability_bandwidth, (int, float))
            or not isfinite(self.probability_bandwidth)
            or not 0 < self.probability_bandwidth <= 0.5
        ):
            raise ValueError("probability_bandwidth must be in (0, 0.5].")
        for name, value, minimum in (
            ("minimum_local_observations", self.minimum_local_observations, 20),
            ("minimum_observations_per_fold", self.minimum_observations_per_fold, 5),
            ("block_size", self.block_size, 2),
            ("minimum_blocks", self.minimum_blocks, 5),
            ("minimum_walk_forward_folds", self.minimum_walk_forward_folds, 2),
            ("bootstrap_replicates", self.bootstrap_replicates, 200),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}.")
        for name, value in (
            ("maximum_fold_rate_spread", self.maximum_fold_rate_spread),
            ("maximum_interval_width", self.maximum_interval_width),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or not 0 < value <= 1
            ):
                raise ValueError(f"{name} must be in (0, 1].")


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower_index = floor(position)
    upper_index = ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight


def estimate_local_probability_interval(
    out_of_sample_predictions: Sequence[dict],
    *,
    target_probability: float,
    policy: UncertaintyPolicy = UncertaintyPolicy(),
    seed: int = 20261119,
) -> dict:
    """Estimate a block-bootstrap interval for the local walk-forward even rate."""
    if (
        isinstance(target_probability, bool)
        or not isinstance(target_probability, (int, float))
        or not isfinite(target_probability)
        or not 0 <= target_probability <= 1
    ):
        raise ValueError("target_probability must be finite and between 0 and 1.")
    if type(seed) is not int:
        raise ValueError("seed must be an integer.")
    if not isinstance(out_of_sample_predictions, (list, tuple)):
        raise ValueError("out_of_sample_predictions must be a list or tuple.")

    records = []
    for item in out_of_sample_predictions:
        if not isinstance(item, dict):
            raise ValueError("Every out-of-sample prediction must be an object.")
        index = item.get("target_index")
        fold = item.get("fold")
        probability = item.get("probability_even")
        actual = item.get("actual_parity")
        if type(index) is not int or index < 0:
            raise ValueError("Every prediction needs a non-negative integer target_index.")
        if type(fold) is not int or fold < 1:
            raise ValueError("Every prediction needs a positive integer fold.")
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise ValueError("probability_even must be finite and between 0 and 1.")
        if actual not in {"even", "odd"}:
            raise ValueError("actual_parity must be even or odd.")
        records.append({
            "target_index": index,
            "fold": fold,
            "probability_even": float(probability),
            "actual_even": 1 if actual == "even" else 0,
        })

    if not records:
        return {
            "method": "walk_forward_moving_block_bootstrap_local_calibration",
            "target_probability": float(target_probability),
            "validated": False,
            "lower_probability": None,
            "upper_probability": None,
            "reason": "No out-of-sample forecasts are available.",
        }

    records.sort(key=lambda item: item["target_index"])
    indices = [item["target_index"] for item in records]
    if len(set(indices)) != len(indices):
        raise ValueError("Out-of-sample target_index values must be unique.")

    local = [
        item for item in records
        if abs(item["probability_even"] - target_probability)
        <= policy.probability_bandwidth
    ]
    local_count = len(local)
    fold_groups: dict[int, list[int]] = {}
    for item in local:
        fold_groups.setdefault(item["fold"], []).append(item["actual_even"])
    fold_rates = {
        fold: sum(labels) / len(labels)
        for fold, labels in fold_groups.items()
        if len(labels) >= policy.minimum_observations_per_fold
    }
    fold_count = len(fold_rates)
    fold_spread = (
        max(fold_rates.values()) - min(fold_rates.values())
        if fold_rates else None
    )

    block_count = len({
        item["target_index"] // policy.block_size for item in local
    })
    base_result = {
        "method": "walk_forward_moving_block_bootstrap_local_calibration",
        "confidence_level": policy.confidence_level,
        "target_probability": float(target_probability),
        "probability_bandwidth": policy.probability_bandwidth,
        "local_sample_size": local_count,
        "local_block_count": block_count,
        "local_fold_count": fold_count,
        "fold_rate_spread": fold_spread,
        "point_estimate": (
            sum(item["actual_even"] for item in local) / local_count
            if local_count else None
        ),
        "validated": False,
        "lower_probability": None,
        "upper_probability": None,
    }

    if local_count < policy.minimum_local_observations:
        return {
            **base_result,
            "reason": "Insufficient out-of-sample observations near the target probability.",
        }
    if block_count < policy.minimum_blocks:
        return {
            **base_result,
            "reason": "Insufficient distinct contiguous blocks for dependence-aware resampling.",
        }
    if fold_count < policy.minimum_walk_forward_folds:
        return {
            **base_result,
            "reason": "Insufficient walk-forward folds with enough local observations.",
        }

    blocks = [
        records[start:start + policy.block_size]
        for start in range(0, len(records), policy.block_size)
    ]
    blocks_to_sample = len(blocks)
    generator = random.Random(seed)
    bootstrap_rates = []
    for _ in range(policy.bootstrap_replicates):
        sampled_local = []
        for _ in range(blocks_to_sample):
            selected_block = blocks[generator.randrange(blocks_to_sample)]
            sampled_local.extend(
                item["actual_even"]
                for item in selected_block
                if abs(item["probability_even"] - target_probability)
                <= policy.probability_bandwidth
            )
        if sampled_local:
            bootstrap_rates.append(sum(sampled_local) / len(sampled_local))

    if len(bootstrap_rates) < max(100, policy.bootstrap_replicates // 2):
        return {
            **base_result,
            "reason": "Too few valid moving-block bootstrap replicates were produced.",
        }

    tail = (1 - policy.confidence_level) / 2
    bootstrap_lower = max(0.0, _quantile(bootstrap_rates, tail))
    bootstrap_upper = min(1.0, _quantile(bootstrap_rates, 1 - tail))
    # The final conservative envelope contains both the model point forecast
    # and the bootstrap interval for observed local calibration outcomes.
    lower = min(float(target_probability), bootstrap_lower)
    upper = max(float(target_probability), bootstrap_upper)
    interval_width = upper - lower
    validated = (
        fold_spread is not None
        and fold_spread <= policy.maximum_fold_rate_spread
        and interval_width <= policy.maximum_interval_width
    )
    return {
        **base_result,
        "lower_probability": lower,
        "upper_probability": upper,
        "bootstrap_lower_probability": bootstrap_lower,
        "bootstrap_upper_probability": bootstrap_upper,
        "interval_width": interval_width,
        "bootstrap_replicates_used": len(bootstrap_rates),
        "validated": validated,
        "reason": (
            "Local calibration interval passed configured sample, block, fold-stability "
            "and width gates."
            if validated
            else "Local calibration interval failed fold-stability or maximum-width gates."
        ),
    }


def select_probability_interval(
    calibration_intervals: Sequence[dict],
    *,
    target_probability: float,
) -> dict:
    """Select the nearest validated local-calibration interval without extrapolation."""
    if (
        isinstance(target_probability, bool)
        or not isinstance(target_probability, (int, float))
        or not isfinite(target_probability)
        or not 0 <= target_probability <= 1
    ):
        raise ValueError("target_probability must be finite and between 0 and 1.")
    eligible = [
        item for item in calibration_intervals
        if isinstance(item, dict)
        and item.get("validated") is True
        and isinstance(item.get("lower_probability"), (int, float))
        and isinstance(item.get("upper_probability"), (int, float))
        and isinstance(item.get("target_probability"), (int, float))
    ]
    if not eligible:
        return {
            "validated": False,
            "lower_probability": None,
            "upper_probability": None,
            "reason": "No validated local-calibration interval is available.",
        }
    selected = min(
        eligible,
        key=lambda item: abs(item["target_probability"] - target_probability),
    )
    if abs(selected["target_probability"] - target_probability) > selected.get(
        "probability_bandwidth", 0
    ):
        return {
            "validated": False,
            "lower_probability": None,
            "upper_probability": None,
            "reason": "Forecast is outside the validated calibration interval bandwidth.",
        }
    return {
        "validated": True,
        "method": selected["method"],
        "confidence_level": selected["confidence_level"],
        "target_probability": float(target_probability),
        "calibration_center": selected["target_probability"],
        "lower_probability": selected["lower_probability"],
        "upper_probability": selected["upper_probability"],
        "reason": "Nearest validated local-calibration interval selected without extrapolation.",
    }


def bootstrap_log_loss_improvement(
    out_of_sample_predictions: Sequence[dict],
    *,
    baseline_key: str,
    confidence_level: float = 0.90,
    block_size: int = 20,
    minimum_observations: int = 200,
    minimum_blocks: int = 10,
    minimum_folds: int = 5,
    bootstrap_replicates: int = 500,
    seed: int = 20261120,
) -> dict:
    """Block-bootstrap baseline-minus-model log-loss improvement.

    Positive values favour the model. Statistical support is deliberately
    conservative: the lower bootstrap bound must be positive and every
    walk-forward fold must show positive mean improvement.
    """
    if baseline_key not in {"constant_probability", "rolling_probability"}:
        raise ValueError("baseline_key must identify a supported baseline.")
    if (
        isinstance(confidence_level, bool)
        or not isinstance(confidence_level, (int, float))
        or not isfinite(confidence_level)
        or not 0.5 < confidence_level < 1
    ):
        raise ValueError("confidence_level must be in (0.5, 1).")
    if type(block_size) is not int or block_size < 2:
        raise ValueError("block_size must be an integer of at least 2.")
    if type(minimum_observations) is not int or minimum_observations < 20:
        raise ValueError("minimum_observations must be an integer of at least 20.")
    if type(minimum_blocks) is not int or minimum_blocks < 5:
        raise ValueError("minimum_blocks must be an integer of at least 5.")
    if type(minimum_folds) is not int or minimum_folds < 2:
        raise ValueError("minimum_folds must be an integer of at least 2.")
    if type(bootstrap_replicates) is not int or bootstrap_replicates < 200:
        raise ValueError("bootstrap_replicates must be an integer of at least 200.")
    if type(seed) is not int:
        raise ValueError("seed must be an integer.")

    records = []
    epsilon = 1e-15
    for item in out_of_sample_predictions:
        if not isinstance(item, dict):
            raise ValueError("Every out-of-sample prediction must be an object.")
        index = item.get("target_index")
        fold = item.get("fold")
        actual = item.get("actual_parity")
        model_probability = item.get("probability_even")
        baseline_probability = item.get(baseline_key)
        if type(index) is not int or index < 0:
            raise ValueError("Every prediction needs a non-negative integer target_index.")
        if type(fold) is not int or fold < 1:
            raise ValueError("Every prediction needs a positive integer fold.")
        if actual not in {"even", "odd"}:
            raise ValueError("actual_parity must be even or odd.")
        for name, value in (
            ("probability_even", model_probability),
            (baseline_key, baseline_probability),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be a finite probability from 0 to 1.")

        target = 1 if actual == "even" else 0
        model_p = min(1 - epsilon, max(epsilon, float(model_probability)))
        baseline_p = min(1 - epsilon, max(epsilon, float(baseline_probability)))
        model_loss = -(
            target * log(model_p) + (1 - target) * log(1 - model_p)
        )
        baseline_loss = -(
            target * log(baseline_p) + (1 - target) * log(1 - baseline_p)
        )
        records.append({
            "target_index": index,
            "fold": fold,
            "improvement": baseline_loss - model_loss,
        })

    if not records:
        return {
            "method": "moving_block_bootstrap_log_loss_difference",
            "baseline": baseline_key,
            "statistically_supported": False,
            "reason": "No out-of-sample forecasts are available.",
        }

    records.sort(key=lambda item: item["target_index"])
    indices = [item["target_index"] for item in records]
    if len(set(indices)) != len(indices):
        raise ValueError("Out-of-sample target_index values must be unique.")

    fold_values: dict[int, list[float]] = {}
    for item in records:
        fold_values.setdefault(item["fold"], []).append(item["improvement"])
    fold_improvements = {
        fold: sum(values) / len(values)
        for fold, values in fold_values.items()
    }
    positive_folds = sum(value > 0 for value in fold_improvements.values())

    blocks = [
        records[start:start + block_size]
        for start in range(0, len(records), block_size)
    ]
    point_improvement = sum(item["improvement"] for item in records) / len(records)
    base_result = {
        "method": "moving_block_bootstrap_log_loss_difference",
        "baseline": baseline_key,
        "sample_size": len(records),
        "block_count": len(blocks),
        "fold_count": len(fold_improvements),
        "minimum_folds_required": minimum_folds,
        "positive_folds": positive_folds,
        "fold_improvements": {
            str(fold): value for fold, value in fold_improvements.items()
        },
        "point_improvement": point_improvement,
        "statistically_supported": False,
    }
    if len(records) < minimum_observations or len(blocks) < minimum_blocks:
        return {
            **base_result,
            "lower_improvement": None,
            "upper_improvement": None,
            "reason": "Insufficient out-of-sample observations or blocks.",
        }

    generator = random.Random(seed)
    bootstrap_means = []
    for _ in range(bootstrap_replicates):
        sampled = []
        for _ in range(len(blocks)):
            sampled.extend(
                item["improvement"]
                for item in blocks[generator.randrange(len(blocks))]
            )
        if sampled:
            bootstrap_means.append(sum(sampled) / len(sampled))

    tail = (1 - confidence_level) / 2
    lower = _quantile(bootstrap_means, tail)
    upper = _quantile(bootstrap_means, 1 - tail)
    approximate_one_sided_p = (
        sum(value <= 0 for value in bootstrap_means) + 1
    ) / (len(bootstrap_means) + 1)
    supported = (
        lower > 0
        and len(fold_improvements) >= minimum_folds
        and positive_folds == len(fold_improvements)
    )
    return {
        **base_result,
        "confidence_level": confidence_level,
        "lower_improvement": lower,
        "upper_improvement": upper,
        "approximate_one_sided_bootstrap_p": approximate_one_sided_p,
        "statistically_supported": supported,
        "reason": (
            "Positive block-bootstrap improvement and positive improvement in every fold."
            if supported
            else (
                "Improvement did not clear the minimum-fold, conservative interval "
                "and all-fold gates."
            )
        ),
    }
