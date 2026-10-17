import pytest

from app.bot_state import (
    TradeSnapshot,
    TradeState,
    may_start_next_trade,
    may_submit_buy,
    transition,
)


def test_buy_requires_a_proposal_id_and_proposal_stage():
    waiting = TradeSnapshot(TradeState.REQUESTING_PROPOSAL)
    assert not may_submit_buy(waiting)
    quoted = TradeSnapshot(TradeState.REQUESTING_PROPOSAL, proposal_id="proposal-123")
    assert may_submit_buy(quoted)
    # Persist the buy attempt before sending it; a retry is no longer allowed.
    buy_pending = transition(quoted, TradeState.BUY_PENDING)
    assert not may_submit_buy(buy_pending)


def test_buy_ack_must_include_contract_before_open_state():
    pending = TradeSnapshot(TradeState.BUY_PENDING, proposal_id="proposal-123", stake=1.0)
    with pytest.raises(ValueError, match="contract_id"):
        transition(pending, TradeState.OPEN)


def test_settlement_requires_confirmed_contract_and_financials():
    opened = TradeSnapshot(TradeState.OPEN, contract_id="987654")
    settling = transition(opened, TradeState.SETTLING)
    with pytest.raises(ValueError, match="payout and profit"):
        transition(settling, TradeState.SETTLED, payout=1.8)
    settled = transition(settling, TradeState.SETTLED, payout=1.8, profit=0.8)
    assert settled.state == TradeState.SETTLED
    assert settled.contract_id == "987654"
    assert may_start_next_trade(settled)


def test_unknown_buy_outcome_blocks_next_trade_until_reconciled():
    pending = TradeSnapshot(TradeState.BUY_PENDING, proposal_id="proposal-123", stake=1.0)
    unknown = transition(pending, TradeState.OUTCOME_UNKNOWN)
    assert not may_start_next_trade(unknown)
    reconciled_open = transition(unknown, TradeState.OPEN, contract_id="987654")
    assert not may_start_next_trade(reconciled_open)
    settled = transition(
        transition(reconciled_open, TradeState.SETTLING),
        TradeState.SETTLED,
        payout=0.0,
        profit=-1.0,
    )
    assert may_start_next_trade(settled)


def test_illegal_transition_cannot_skip_buy_ack_or_settlement():
    created = TradeSnapshot(TradeState.CREATED)
    with pytest.raises(ValueError, match="Illegal"):
        transition(created, TradeState.OPEN, contract_id="987654")


def test_unknown_outcome_is_not_used_for_pre_buy_proposal_timeout():
    requesting = TradeSnapshot(TradeState.REQUESTING_PROPOSAL)
    with pytest.raises(ValueError, match="Illegal"):
        transition(requesting, TradeState.OUTCOME_UNKNOWN)


def test_only_confirmed_settlement_counts_as_completed_trade():
    for state in (
        TradeState.CREATED,
        TradeState.REQUESTING_PROPOSAL,
        TradeState.BUY_PENDING,
        TradeState.OPEN,
        TradeState.SETTLING,
        TradeState.OUTCOME_UNKNOWN,
    ):
        assert not may_start_next_trade(TradeSnapshot(state))
