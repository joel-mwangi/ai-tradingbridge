import pytest

from app.even_odd.entry_detector import (
    EntryObservation,
    EntryPolicy,
    evaluate_hybrid_entry,
)


def policy():
    return EntryPolicy(
        minimum_sample_size=1000,
        maximum_data_age_seconds=3,
        required_confirmations=3,
        maximum_confirmation_gap_seconds=10,
        minimum_edge_over_break_even=0.05,
        minimum_conservative_expected_value=0.1,
    )


def observation(
    epoch,
    *,
    p_even=0.8,
    lower=0.75,
    upper=0.85,
    validated=True,
    age=1.0,
    sample=1200,
    version="v1",
):
    return EntryObservation(
        tick_epoch=epoch,
        sample_size=sample,
        data_age_seconds=age,
        even_probability=p_even,
        lower_even_probability=lower,
        upper_even_probability=upper,
        even_profit_if_win=0.8,
        even_loss_if_lose=1.0,
        odd_profit_if_win=0.9,
        odd_loss_if_lose=1.0,
        model_id="model-1",
        model_version=version,
        model_validated=validated,
        uncertainty_method="block_bootstrap",
        uncertainty_level=0.9,
        uncertainty_validated=True,
    )


def test_hybrid_entry_requires_consistent_fresh_validated_payout_edge():
    result = evaluate_hybrid_entry(
        [observation(100), observation(105), observation(110)],
        policy=policy(),
    )
    assert result["status"] == "candidate"
    assert result["side"] == "even"
    assert result["confirmations"] == 3
    assert result["conservative_expected_value"] > 0
    assert result["execution_authorized"] is False


def test_hybrid_entry_waits_for_validated_model():
    result = evaluate_hybrid_entry(
        [observation(100, validated=False), observation(105), observation(110)],
        policy=policy(),
    )
    assert result["status"] == "wait"
    assert "not passed" in result["reason"]
    assert result["execution_authorized"] is False


def test_hybrid_entry_rejects_direction_changes_and_model_version_changes():
    direction_change = evaluate_hybrid_entry(
        [
            observation(100),
            observation(105, p_even=0.2, lower=0.15, upper=0.25),
            observation(110),
        ],
        policy=policy(),
    )
    assert direction_change["status"] == "wait"
    assert "direction changed" in direction_change["reason"]

    version_change = evaluate_hybrid_entry(
        [observation(100), observation(105, version="v2"), observation(110)],
        policy=policy(),
    )
    assert version_change["status"] == "wait"
    assert "version changed" in version_change["reason"]


@pytest.mark.parametrize(
    "items",
    [
        [observation(100, age=4), observation(105), observation(110)],
        [observation(100, sample=999), observation(105), observation(110)],
        [observation(100), observation(111), observation(120)],
    ],
)
def test_hybrid_entry_rejects_stale_small_or_expired_confirmations(items):
    result = evaluate_hybrid_entry(items, policy=policy())
    assert result["status"] == "wait"


def test_hybrid_entry_waits_when_probability_edge_or_conservative_ev_is_insufficient():
    result = evaluate_hybrid_entry(
        [
            observation(100, p_even=0.6, lower=0.56, upper=0.64),
            observation(105, p_even=0.6, lower=0.56, upper=0.64),
            observation(110, p_even=0.6, lower=0.56, upper=0.64),
        ],
        policy=policy(),
    )
    assert result["status"] == "wait"


def test_hybrid_entry_can_select_odd_using_side_specific_payout():
    result = evaluate_hybrid_entry(
        [
            observation(100, p_even=0.2, lower=0.15, upper=0.25),
            observation(105, p_even=0.2, lower=0.15, upper=0.25),
            observation(110, p_even=0.2, lower=0.15, upper=0.25),
        ],
        policy=policy(),
    )
    assert result["status"] == "candidate"
    assert result["side"] == "odd"


def test_hybrid_entry_rejects_invalid_probability_interval_and_boolean_values():
    with pytest.raises(ValueError, match="interval"):
        evaluate_hybrid_entry(
            [
                observation(100, p_even=0.8, lower=0.9, upper=0.95),
                observation(105),
                observation(110),
            ],
            policy=policy(),
        )
    with pytest.raises(ValueError, match="numeric"):
        evaluate_hybrid_entry(
            [
                observation(100, p_even=True),
                observation(105),
                observation(110),
            ],
            policy=policy(),
        )



def test_hybrid_entry_waits_for_unvalidated_probability_uncertainty():
    observations = [
        observation(100),
        observation(105),
        observation(110),
    ]
    observations[1] = EntryObservation(
        **{
            **observations[1].__dict__,
            "uncertainty_validated": False,
        }
    )
    result = evaluate_hybrid_entry(observations, policy=policy())
    assert result["status"] == "wait"
    assert "uncertainty bounds" in result["reason"]
