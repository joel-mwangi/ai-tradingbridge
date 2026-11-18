"""Conservative hybrid entry confirmation for Even/Odd opportunities.

This module consumes validated model probability intervals and payout terms.
It never places orders and always returns execution_authorized=False.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence


@dataclass(frozen=True, slots=True)
class EntryPolicy:
    minimum_sample_size: int
    maximum_data_age_seconds: float = 10.0
    required_confirmations: int = 3
    maximum_confirmation_gap_seconds: float = 10.0
    minimum_edge_over_break_even: float = 0.05
    minimum_conservative_expected_value: float = 0.10

    def __post_init__(self) -> None:
        if type(self.minimum_sample_size) is not int or self.minimum_sample_size < 20:
            raise ValueError("minimum_sample_size must be an integer of at least 20.")
        if (
            isinstance(self.maximum_data_age_seconds, bool)
            or not isinstance(self.maximum_data_age_seconds, (int, float))
            or not isfinite(self.maximum_data_age_seconds)
            or self.maximum_data_age_seconds <= 0
        ):
            raise ValueError("maximum_data_age_seconds must be finite and positive.")
        if type(self.required_confirmations) is not int or not 2 <= self.required_confirmations <= 10:
            raise ValueError("required_confirmations must be between 2 and 10.")
        if (
            isinstance(self.maximum_confirmation_gap_seconds, bool)
            or not isinstance(self.maximum_confirmation_gap_seconds, (int, float))
            or not isfinite(self.maximum_confirmation_gap_seconds)
            or self.maximum_confirmation_gap_seconds <= 0
        ):
            raise ValueError("maximum_confirmation_gap_seconds must be finite and positive.")
        if (
            isinstance(self.minimum_edge_over_break_even, bool)
            or not isinstance(self.minimum_edge_over_break_even, (int, float))
            or not isfinite(self.minimum_edge_over_break_even)
            or not 0 <= self.minimum_edge_over_break_even < 1
        ):
            raise ValueError("minimum_edge_over_break_even must be in [0, 1).")
        if (
            isinstance(self.minimum_conservative_expected_value, bool)
            or not isinstance(self.minimum_conservative_expected_value, (int, float))
            or not isfinite(self.minimum_conservative_expected_value)
            or self.minimum_conservative_expected_value < 0
        ):
            raise ValueError("minimum_conservative_expected_value must be finite and non-negative.")


@dataclass(frozen=True, slots=True)
class EntryObservation:
    tick_row_id: int
    tick_epoch: int
    sample_size: int
    data_age_seconds: float
    even_probability: float
    lower_even_probability: float
    upper_even_probability: float
    even_profit_if_win: float
    even_loss_if_lose: float
    odd_profit_if_win: float
    odd_loss_if_lose: float
    model_id: str
    model_version: str
    model_validated: bool
    uncertainty_method: str
    uncertainty_level: float
    uncertainty_validated: bool
    risk_budget_available: bool


def _validate_observation(observation: EntryObservation) -> None:
    if type(observation.tick_row_id) is not int or observation.tick_row_id <= 0:
        raise ValueError("tick_row_id must be a positive integer.")
    if type(observation.tick_epoch) is not int or observation.tick_epoch <= 0:
        raise ValueError("tick_epoch must be a positive integer.")
    if type(observation.sample_size) is not int or observation.sample_size < 1:
        raise ValueError("sample_size must be a positive integer.")
    if (
        isinstance(observation.data_age_seconds, bool)
        or not isinstance(observation.data_age_seconds, (int, float))
        or not isfinite(observation.data_age_seconds)
        or observation.data_age_seconds < 0
    ):
        raise ValueError("data_age_seconds must be finite and non-negative.")
    for name, value in (
        ("even_probability", observation.even_probability),
        ("lower_even_probability", observation.lower_even_probability),
        ("upper_even_probability", observation.upper_even_probability),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric.")
        if not isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be a finite probability from 0 to 1.")
    if not (
        observation.lower_even_probability
        <= observation.even_probability
        <= observation.upper_even_probability
    ):
        raise ValueError("Probability interval must contain the point forecast.")
    for name, value in (
        ("even_profit_if_win", observation.even_profit_if_win),
        ("even_loss_if_lose", observation.even_loss_if_lose),
        ("odd_profit_if_win", observation.odd_profit_if_win),
        ("odd_loss_if_lose", observation.odd_loss_if_lose),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric.")
        if not isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive.")
    if (
        not isinstance(observation.model_id, str) or not observation.model_id.strip()
        or not isinstance(observation.model_version, str) or not observation.model_version.strip()
    ):
        raise ValueError("Model identity and version are required.")
    if type(observation.model_validated) is not bool:
        raise ValueError("model_validated must be boolean.")
    if not isinstance(observation.uncertainty_method, str) or not observation.uncertainty_method.strip():
        raise ValueError("A probability uncertainty method is required.")
    if (
        isinstance(observation.uncertainty_level, bool)
        or not isinstance(observation.uncertainty_level, (int, float))
        or not isfinite(observation.uncertainty_level)
        or not 0.5 < observation.uncertainty_level < 1
    ):
        raise ValueError("uncertainty_level must be a finite confidence level in (0.5, 1).")
    if type(observation.uncertainty_validated) is not bool:
        raise ValueError("uncertainty_validated must be boolean.")


def _conservative_side(observation: EntryObservation) -> dict:
    even_break_even = observation.even_loss_if_lose / (
        observation.even_profit_if_win + observation.even_loss_if_lose
    )
    odd_break_even = observation.odd_loss_if_lose / (
        observation.odd_profit_if_win + observation.odd_loss_if_lose
    )
    even_lower = observation.lower_even_probability
    odd_lower = 1.0 - observation.upper_even_probability
    even_ev = (
        even_lower * observation.even_profit_if_win
        - (1 - even_lower) * observation.even_loss_if_lose
    )
    odd_ev = (
        odd_lower * observation.odd_profit_if_win
        - (1 - odd_lower) * observation.odd_loss_if_lose
    )

    if even_ev >= odd_ev:
        return {
            "side": "even",
            "lower_probability": even_lower,
            "break_even_probability": even_break_even,
            "edge_over_break_even": even_lower - even_break_even,
            "conservative_expected_value": even_ev,
        }
    return {
        "side": "odd",
        "lower_probability": odd_lower,
        "break_even_probability": odd_break_even,
        "edge_over_break_even": odd_lower - odd_break_even,
        "conservative_expected_value": odd_ev,
    }


def evaluate_hybrid_entry(
    observations: Sequence[EntryObservation],
    *,
    policy: EntryPolicy,
) -> dict:
    """Require fresh, stable, payout-positive evidence across distinct snapshots."""
    if not observations:
        raise ValueError("At least one entry observation is required.")
    if any(not isinstance(item, EntryObservation) for item in observations):
        raise ValueError("All observations must be EntryObservation instances.")

    required = policy.required_confirmations
    if len(observations) < required:
        return {
            "status": "wait",
            "reason": f"Waiting for {required - len(observations)} more confirmations.",
            "execution_authorized": False,
        }

    recent = list(observations[-required:])
    for observation in recent:
        _validate_observation(observation)
        if not observation.model_validated:
            return {
                "status": "wait",
                "reason": "Forecasting model has not passed independent validation.",
                "execution_authorized": False,
            }
        if not observation.uncertainty_validated:
            return {
                "status": "wait",
                "reason": "Probability uncertainty bounds have not been independently validated.",
                "execution_authorized": False,
            }
        if observation.sample_size < policy.minimum_sample_size:
            return {
                "status": "wait",
                "reason": "The latest forecast does not have enough historical observations.",
                "execution_authorized": False,
            }
        if observation.data_age_seconds > policy.maximum_data_age_seconds:
            return {
                "status": "wait",
                "reason": "Market data is stale.",
                "execution_authorized": False,
            }

    model_ids = {(item.model_id, item.model_version) for item in recent}
    if len(model_ids) != 1:
        return {
            "status": "wait",
            "reason": "Model identity or version changed during confirmation.",
            "execution_authorized": False,
        }

    for previous, current in zip(recent, recent[1:]):
        if current.tick_row_id <= previous.tick_row_id or current.tick_epoch < previous.tick_epoch:
            return {
                "status": "wait",
                "reason": "Confirmation snapshots are not strictly chronological.",
                "execution_authorized": False,
            }
        gap = current.tick_epoch - previous.tick_epoch
        if gap > policy.maximum_confirmation_gap_seconds:
            return {
                "status": "wait",
                "reason": "Confirmation sequence expired before enough stable observations arrived.",
                "execution_authorized": False,
            }

    side_evidence = [_conservative_side(item) for item in recent]
    sides = {item["side"] for item in side_evidence}
    if len(sides) != 1:
        return {
            "status": "wait",
            "reason": "Model direction changed during the confirmation window.",
            "execution_authorized": False,
        }

    latest = side_evidence[-1]
    if any(
        item["edge_over_break_even"] < policy.minimum_edge_over_break_even
        for item in side_evidence
    ):
        return {
            "status": "wait",
            "reason": "At least one confirmation has insufficient conservative probability edge.",
            "execution_authorized": False,
        }
    if any(
        item["conservative_expected_value"]
        <= policy.minimum_conservative_expected_value
        for item in side_evidence
    ):
        return {
            "status": "wait",
            "reason": "At least one confirmation has insufficient payout-adjusted conservative EV.",
            "execution_authorized": False,
        }

    return {
        "status": "candidate",
        "side": latest["side"],
        "model_id": recent[-1].model_id,
        "model_version": recent[-1].model_version,
        "uncertainty_method": recent[-1].uncertainty_method,
        "uncertainty_level": recent[-1].uncertainty_level,
        "confirmations": required,
        "lower_probability": latest["lower_probability"],
        "break_even_probability": latest["break_even_probability"],
        "edge_over_break_even": latest["edge_over_break_even"],
        "conservative_expected_value": latest["conservative_expected_value"],
        "latest_tick_epoch": recent[-1].tick_epoch,
        "latest_tick_row_id": recent[-1].tick_row_id,
        "execution_authorized": False,
        "reason": (
            "Fresh, consistent, validated forecasts passed the configured "
            "payout-aware confirmation gates. This is a research candidate only."
        ),
    }
