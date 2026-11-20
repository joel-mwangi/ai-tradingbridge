import pytest

from app.even_odd.ml.payout_replay import replay_payout_aware_strategy


def quote(epoch, *, ask=1.0, payout=1.95):
    return {
        "spot_time": epoch,
        "even_ask_price": ask,
        "even_payout": payout,
        "odd_ask_price": ask,
        "odd_payout": payout,
    }


def prediction(epoch, actual="even", *, lower=0.8, upper=0.9, valid=True, fold=1):
    return {
        "target_epoch": epoch,
        "fold": fold,
        "actual_parity": actual,
        "probability_even": (lower + upper) / 2,
        "lower_even_probability": lower,
        "upper_even_probability": upper,
        "uncertainty_validated": valid,
    }


def test_replay_uses_only_prior_matched_quote_and_reports_realized_metrics():
    result = replay_payout_aware_strategy(
        [
            prediction(100, "even"),
            prediction(110, "odd", lower=0.1, upper=0.2),
            prediction(130, "even", valid=False),
        ],
        [quote(100), quote(110), quote(131)],
        stake=1.0,
        maximum_quote_age_seconds=10,
        minimum_trades_for_evidence=2,
    )
    assert result["opportunities"] == 3
    assert result["quote_matched_opportunities"] == 2
    assert result["entry_count"] == 2
    assert result["wins"] == 2
    assert result["losses"] == 0
    assert result["net_profit"] == pytest.approx(1.9)
    assert result["entry_coverage"] == pytest.approx(2 / 3)
    assert result["evidence_sample_sufficient"] is True
    assert result["validated"] is False
    assert result["execution_authorized"] is False


def test_replay_does_not_use_future_or_stale_quote_snapshots():
    result = replay_payout_aware_strategy(
        [prediction(100), prediction(120)],
        [quote(101), quote(90)],
        stake=1.0,
        maximum_quote_age_seconds=5,
    )
    assert result["quote_matched_opportunities"] == 0
    assert result["entry_count"] == 0
    assert result["net_profit"] is None


def test_replay_rejects_invalid_quote_economics_and_probability_intervals():
    with pytest.raises(ValueError, match="payout must exceed"):
        replay_payout_aware_strategy(
            [prediction(100)],
            [quote(100, payout=0.9)],
            stake=1.0,
        )

def test_replay_excludes_multiple_ticks_sharing_second_resolution_quote_time():
    result = replay_payout_aware_strategy(
        [prediction(100), prediction(100), prediction(101)],
        [quote(100), quote(101)],
        stake=1.0,
    )
    assert result["opportunities"] == 3
    assert result["ambiguous_timestamp_opportunities"] == 2
    assert result["unambiguous_timestamp_opportunities"] == 1
    assert result["entry_count"] == 1

def test_replay_estimates_same_side_persistence_only_with_sufficient_oos_folds():
    predictions = []
    quotes = []
    epoch = 1_800_000_000
    for fold in range(1, 6):
        for _ in range(100):
            predictions.append(prediction(epoch, fold=fold))
            quotes.append(quote(epoch))
            epoch += 1
    result = replay_payout_aware_strategy(
        predictions,
        quotes,
        stake=1.0,
        minimum_trades_for_evidence=200,
    )
    assert result["evidence_sample_sufficient"] is True
    assert result["positive_fold_rate"] == 1.0
    horizon_one = result["sequence_persistence_by_horizon"]["1"]
    assert horizon_one["validated"] is True
    assert horizon_one["fold_count"] == 5
    assert horizon_one["trials"] == 495
    assert horizon_one["persistence_rate"] == 1.0
    assert horizon_one["lower_confidence_bound_90"] > 0.9


def test_replay_does_not_claim_persistence_from_fewer_than_five_folds():
    predictions = []
    quotes = []
    epoch = 1_800_000_000
    for fold in range(1, 3):
        for _ in range(150):
            predictions.append(prediction(epoch, fold=fold))
            quotes.append(quote(epoch))
            epoch += 1
    result = replay_payout_aware_strategy(predictions, quotes, stake=1.0)
    horizon_one = result["sequence_persistence_by_horizon"]["1"]
    assert horizon_one["trials"] >= 200
    assert horizon_one["validated"] is False
    assert horizon_one["lower_confidence_bound_90"] is None
