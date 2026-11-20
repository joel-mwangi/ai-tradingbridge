import pytest

from app.even_odd import (
    CandidateTradeForecast,
    EvenOddPolicy,
    Revalidation,
    SequenceManager,
    TradeOutcome,
    select_sequence_length,
)


def forecast(
    trade_number: int,
    *,
    p: float = 0.60,
    lower: float = 0.56,
    signal_valid: bool = True,
    model_validated: bool = True,
    uncertainty_validated: bool = True,
    risk_budget_available: bool = True,
    model_id: str = "test-model",
    model_version: str = "1",
    opportunity_persistence_lower_bound: float = 0.9,
    opportunity_persistence_validated: bool = True,
    historical_oos_evidence_rows: int = 500,
    historical_oos_positive_fold_rate: float = 0.8,
    historical_oos_expected_value: float = 0.12,
    historical_oos_net_profit: float = 20.0,
    historical_oos_roi: float = 0.1,
) -> CandidateTradeForecast:
    return CandidateTradeForecast(
        trade_number=trade_number,
        win_probability=p,
        lower_bound_win_probability=lower,
        profit_if_win=0.8,
        loss_if_lose=1.0,
        signal_valid=signal_valid,
        model_validated=model_validated,
        uncertainty_validated=uncertainty_validated,
        risk_budget_available=risk_budget_available,
        model_id=model_id,
        model_version=model_version,
        opportunity_persistence_lower_bound=opportunity_persistence_lower_bound,
        opportunity_persistence_validated=opportunity_persistence_validated,
        historical_oos_evidence_rows=historical_oos_evidence_rows,
        historical_oos_positive_fold_rate=historical_oos_positive_fold_rate,
        historical_oos_expected_value=historical_oos_expected_value,
        historical_oos_net_profit=historical_oos_net_profit,
        historical_oos_roi=historical_oos_roi,
        historical_oos_net_profit=20.0,
        historical_oos_roi=0.1,
    )


def valid_revalidation(**overrides) -> Revalidation:
    values = {
        "model_validated": True,
        "uncertainty_validated": True,
        "signal_valid": True,
        "data_fresh": True,
        "conservative_expected_value": 0.12,
        "risk_budget_available": True,
    }
    values.update(overrides)
    return Revalidation(**values)


def test_policy_uses_user_approved_limits():
    policy = EvenOddPolicy()
    assert policy.max_trades_per_sequence == 7
    assert policy.max_consecutive_losses == 2
    assert policy.max_trades_per_session == 10
    assert policy.fixed_stake is True
    assert policy.martingale_enabled is False


def test_sequence_optimizer_selects_up_to_seven_from_validated_forecasts():
    decision = select_sequence_length([forecast(i) for i in range(1, 9)])
    assert decision.approved is True
    assert decision.selected_trades == 7
    assert decision.max_allowed_trades == 7


def test_sequence_optimizer_stops_at_first_non_positive_conservative_ev():
    # p_lower=0.52 with 0.8 profit and 1.0 loss gives negative conservative EV.
    candidates = [forecast(1), forecast(2, lower=0.52), forecast(3)]
    decision = select_sequence_length(candidates)
    assert decision.selected_trades == 1
    assert "trade 2" in decision.reason


def test_unvalidated_model_cannot_authorize_any_trade():
    decision = select_sequence_length([forecast(1, model_validated=False)])
    assert decision.approved is False
    assert decision.selected_trades == 0


def test_sequence_optimizer_rejects_model_version_change():
    candidates = [forecast(1), forecast(2, model_version="2"), forecast(3)]
    decision = select_sequence_length(candidates)
    assert decision.selected_trades == 1
    assert "identity/version changed" in decision.reason


def test_sequence_is_capped_by_remaining_session_capacity():
    decision = select_sequence_length(
        [forecast(i) for i in range(1, 8)],
        session_trades_remaining=2,
    )
    assert decision.selected_trades == 2


def test_two_consecutive_losses_stop_sequence():
    manager = SequenceManager()
    manager.start_sequence(7)
    manager.record_outcome(TradeOutcome.LOSS)
    state = manager.record_outcome(TradeOutcome.LOSS)
    assert state.stopped is True
    assert state.stop_reason == "Two consecutive losses."
    assert manager.can_execute_next(valid_revalidation())[0] is False


def test_win_resets_consecutive_loss_counter():
    manager = SequenceManager()
    manager.start_sequence(5)
    manager.record_outcome(TradeOutcome.LOSS)
    state = manager.record_outcome(TradeOutcome.WIN)
    assert state.consecutive_losses == 0
    assert state.stopped is False


@pytest.mark.parametrize(
    ("changes", "expected_fragment"),
    [
        ({"model_validated": False}, "not validated"),
        ({"uncertainty_validated": False}, "uncertainty is not validated"),
        ({"data_fresh": False}, "stale"),
        ({"signal_valid": False}, "no longer valid"),
        ({"conservative_expected_value": 0.0}, "not positive"),
        ({"risk_budget_available": False}, "exhausted"),
    ],
)
def test_revalidation_vetoes_next_trade(changes, expected_fragment):
    manager = SequenceManager()
    manager.start_sequence(3)
    allowed, reason = manager.can_execute_next(valid_revalidation(**changes))
    assert allowed is False
    assert expected_fragment in reason


def test_session_stops_after_ten_completed_trades():
    manager = SequenceManager()
    for _ in range(2):
        manager.start_sequence(5)
        for _ in range(5):
            manager.record_outcome(TradeOutcome.WIN)
        assert manager.active_sequence.stopped is True
    assert manager.session_trades == 10
    with pytest.raises(ValueError, match="Session trade limit"):
        manager.start_sequence(1)


def test_sequence_length_cannot_exceed_seven():
    manager = SequenceManager()
    with pytest.raises(ValueError, match="between 1 and 7"):
        manager.start_sequence(8)


def test_unknown_outcome_is_rejected_without_mutating_counters():
    manager = SequenceManager()
    state = manager.start_sequence(2)
    with pytest.raises(ValueError, match="confirmed TradeOutcome"):
        manager.record_outcome("unknown")
    assert state.completed_trades == 0
    assert manager.session_trades == 0



def test_sequence_optimizer_rejects_unvalidated_probability_uncertainty():
    decision = select_sequence_length([
        forecast(1, uncertainty_validated=False),
        forecast(2),
    ])
    assert decision.approved is False
    assert decision.selected_trades == 0
    assert "uncertainty" in decision.reason



def test_sequence_policy_and_revalidation_reject_boolean_coercion():
    with pytest.raises(ValueError, match="integer"):
        EvenOddPolicy(max_trades_per_sequence=True)
    with pytest.raises(ValueError, match="must be boolean"):
        Revalidation(
            model_validated="true",
            uncertainty_validated=True,
            signal_valid=True,
            data_fresh=True,
            conservative_expected_value=0.2,
            risk_budget_available=True,
        )



def test_revalidation_rejects_non_finite_expected_value_at_construction():
    with pytest.raises(ValueError, match="finite and numeric"):
        valid_revalidation(conservative_expected_value=float("nan"))



def test_sequence_optimizer_rejects_insufficient_session_loss_budget():
    decision = select_sequence_length([
        forecast(1, risk_budget_available=False),
        forecast(2),
    ])
    assert decision.approved is False
    assert decision.selected_trades == 0
    assert "loss budget" in decision.reason

def test_sequence_optimizer_requires_historical_oos_payout_evidence():
    decision = select_sequence_length([
        forecast(1, historical_oos_evidence_rows=199),
    ])
    assert decision.selected_trades == 0
    assert "Insufficient historical out-of-sample payout evidence" in decision.reason


def test_sequence_optimizer_requires_validated_opportunity_persistence():
    decision = select_sequence_length([
        forecast(1, opportunity_persistence_validated=False),
    ])
    assert decision.selected_trades == 0
    assert "persistence is not validated" in decision.reason


def test_sequence_optimizer_adjusts_ev_by_conservative_persistence_bound():
    decision = select_sequence_length([
        forecast(1, opportunity_persistence_lower_bound=0.5),
        forecast(2, opportunity_persistence_lower_bound=0.25),
    ])
    assert decision.selected_trades == 2
    assert decision.persistence_lower_bound_by_trade == (0.5, 0.25)
    assert decision.sequence_adjusted_conservative_ev_by_trade[1] == pytest.approx(
        decision.conservative_ev_by_trade[1] * 0.25
    )

def test_sequence_optimizer_requires_positive_realized_oos_pnl_and_roi():
    decision = select_sequence_length([
        forecast(1, historical_oos_net_profit=-1.0),
    ])
    assert decision.selected_trades == 0
    assert "not consistently positive" in decision.reason
