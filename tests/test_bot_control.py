"""Unit tests for bot configuration schema validation.

The DB-queue execution pattern has been removed; these tests verify that
DemoBotConfig.validate_contract() correctly accepts and rejects configs.
"""
import pytest
from fastapi import HTTPException

from app.bot_schema import DemoBotConfig, FixedStakeConfig, StopCondition


def _make_config(**overrides) -> DemoBotConfig:
    """Return a minimal valid DIGITEVEN config with optional field overrides."""
    base = {
        "id": "bot-1",
        "name": "Test bot",
        "symbol": "1HZ100V",
        "contractType": "DIGITEVEN",
        "barrier": None,
        "duration": 5,
        "durationUnit": "t",
        "stakeStrategy": {"type": "fixed", "amount": 1.0},
        "stopConditions": [{"type": "tradeCount", "max": 10}],
        "cooldownSeconds": 2,
        "maxTradesPerDay": 10,
        "maxConsecutiveLosses": 3,
    }
    base.update(overrides)
    return DemoBotConfig.model_validate(base)


def test_valid_digiteven_config_passes_validation():
    config = _make_config()
    config.validate_contract()  # must not raise


def test_valid_digitover_with_barrier_passes_validation():
    config = _make_config(contractType="DIGITOVER", barrier="3")
    config.validate_contract()


def test_valid_digitunder_with_barrier_passes_validation():
    config = _make_config(contractType="DIGITUNDER", barrier="5")
    config.validate_contract()


def test_digitover_requires_barrier():
    config = _make_config(contractType="DIGITOVER", barrier=None)
    with pytest.raises(HTTPException) as exc:
        config.validate_contract()
    assert exc.value.status_code == 422
    assert "Barrier" in exc.value.detail


def test_digitover_barrier_max_is_8():
    config = _make_config(contractType="DIGITOVER", barrier="9")
    with pytest.raises(HTTPException) as exc:
        config.validate_contract()
    assert exc.value.status_code == 422
    assert "0" in exc.value.detail or "8" in exc.value.detail


def test_digitunder_barrier_min_is_1():
    config = _make_config(contractType="DIGITUNDER", barrier="0")
    with pytest.raises(HTTPException) as exc:
        config.validate_contract()
    assert exc.value.status_code == 422


def test_digiteven_must_not_have_barrier():
    config = _make_config(contractType="DIGITEVEN", barrier="5")
    with pytest.raises(HTTPException) as exc:
        config.validate_contract()
    assert exc.value.status_code == 422
    assert "Barrier" in exc.value.detail


def test_unsupported_contract_type_raises():
    config = _make_config(contractType="CALL")
    with pytest.raises(HTTPException) as exc:
        config.validate_contract()
    assert exc.value.status_code == 422
    assert "Unsupported" in exc.value.detail


def test_missing_stop_condition_field_raises():
    # profit stop without target
    config = _make_config(
        stopConditions=[{"type": "profit"}]
    )
    with pytest.raises(HTTPException) as exc:
        config.validate_contract()
    assert exc.value.status_code == 422
    assert "target" in exc.value.detail


def test_config_requires_at_least_one_loss_or_count_or_time_stop():
    # Only a profit stop — no hard limit on the downside
    config = _make_config(
        stopConditions=[{"type": "profit", "target": 10.0}]
    )
    with pytest.raises(HTTPException) as exc:
        config.validate_contract()
    assert exc.value.status_code == 422
    assert "loss" in exc.value.detail or "limit" in exc.value.detail


def test_combined_stop_conditions_accepted():
    config = _make_config(
        stopConditions=[
            {"type": "profit", "target": 5.0},
            {"type": "loss", "limit": 3.0},
        ]
    )
    config.validate_contract()  # must not raise


def test_stake_must_be_positive_and_within_limit():
    with pytest.raises(Exception):
        _make_config(stakeStrategy={"type": "fixed", "amount": 0})
    with pytest.raises(Exception):
        _make_config(stakeStrategy={"type": "fixed", "amount": 1001})
