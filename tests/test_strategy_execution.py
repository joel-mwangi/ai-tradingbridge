from app.strategy.digit_strategy import Group
from app.strategy.execution import ExecutionState, StrategyExecution


def test_execution_waits_for_settlement_before_next_buy():
    execution = StrategyExecution("1HZ100V", Group.OVER, barrier=3)

    proposal = execution.begin_signal(1)
    assert proposal["contract_type"] == "DIGITOVER"
    assert execution.state is ExecutionState.WAITING_PROPOSAL

    buy = execution.on_proposal(1, {"id": "proposal-1", "ask_price": 1.0}, 2)
    assert buy == {"buy": "proposal-1", "price": 1.0, "req_id": 2}
    assert execution.state is ExecutionState.WAITING_BUY

    subscribe = execution.on_buy(2, {"buy": {"contract_id": "contract-1"}})
    assert subscribe["contract_id"] == "contract-1"
    assert execution.state is ExecutionState.WAITING_SETTLEMENT
    assert execution.buy_count == 1

    assert execution.on_settlement("contract-1", "won", 0.95, 3)["req_id"] == 3
    assert execution.state is ExecutionState.WAITING_PROPOSAL
    assert execution.buy_count == 1


def test_two_consecutive_losses_stop_before_third_buy():
    execution = StrategyExecution("1HZ100V", Group.UNDER, barrier=6)

    proposal = execution.begin_signal(1)
    buy = execution.on_proposal(1, {"id": "proposal-1", "ask_price": 1.0}, 2)
    execution.on_buy(2, {"buy": {"contract_id": "contract-1"}})
    next_proposal = execution.on_settlement("contract-1", "lost", -1.0, 3)
    assert next_proposal is not None

    buy = execution.on_proposal(3, {"id": "proposal-2", "ask_price": 1.0}, 4)
    execution.on_buy(4, {"buy": {"contract_id": "contract-2"}})
    assert execution.on_settlement("contract-2", "lost", -1.0, 5) is None
    assert execution.state is ExecutionState.STOPPED
    assert execution.buy_count == 2
    assert execution.consecutive_losses == 2


def test_execution_stops_after_three_buys_even_when_profitable():
    execution = StrategyExecution("1HZ100V", Group.OVER, barrier=3)
    execution.begin_signal(1)

    for proposal_request, buy_request, contract_id in ((1, 2, "c1"), (3, 4, "c2"), (5, 6, "c3")):
        execution.on_proposal(proposal_request, {"id": f"p{proposal_request}", "ask_price": 1.0}, buy_request)
        execution.on_buy(buy_request, {"buy": {"contract_id": contract_id}})
        result = execution.on_settlement(contract_id, "won", 1.0, proposal_request + 2)

    assert result is None
    assert execution.state is ExecutionState.COMPLETED
    assert execution.buy_count == 3
