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


# ── New contract-type tests ────────────────────────────────────────────────

def test_status_includes_contract_type_field():
    """status() must include contract_type for all execution objects."""
    execution = StrategyExecution("1HZ100V", Group.OVER, barrier=4)
    execution.begin_signal(1)
    s = execution.status("test")
    assert s["contract_type"] == "DIGITOVER"


def test_from_scan_result_even_omits_barrier_in_proposal():
    """DIGITEVEN proposal must not include a barrier field."""
    from app.strategy.market_scanner import ScanResult, ContractSpec
    spec = ContractSpec("1HZ100V", "DIGITEVEN", None, "even")
    scan = ScanResult(spec, 80.0, 4, 12.5, "HIGH", True, True, "Digit 4 at 12.5%", 0.0)
    execution = StrategyExecution.from_scan_result(scan, stake=1.0, currency="USD", duration=1)
    assert execution.contract_type == "DIGITEVEN"
    proposal = execution._proposal(1)
    assert proposal["contract_type"] == "DIGITEVEN"
    assert "barrier" not in proposal


def test_from_scan_result_digitdiff_includes_barrier_in_proposal():
    """DIGITDIFF proposal must include the target digit as barrier."""
    from app.strategy.market_scanner import ScanResult, ContractSpec
    spec = ContractSpec("R_100", "DIGITDIFF", 3, "differs")
    scan = ScanResult(spec, 75.0, 3, 6.5, "MEDIUM", True, False, "Digit 3 at 6.5%", 0.0)
    execution = StrategyExecution.from_scan_result(scan, stake=1.0, currency="USD", duration=1)
    assert execution.contract_type == "DIGITDIFF"
    proposal = execution._proposal(1)
    assert proposal["contract_type"] == "DIGITDIFF"
    assert proposal["barrier"] == 3


def test_from_scan_result_status_shows_correct_contract_type():
    """status() on a DIGITMATCH execution must report contract_type=DIGITMATCH."""
    from app.strategy.market_scanner import ScanResult, ContractSpec
    spec = ContractSpec("1HZ75V", "DIGITMATCH", 7, "matches")
    scan = ScanResult(spec, 85.0, 7, 14.2, "HIGH", True, True, "Digit 7 at 14.2%", 0.0)
    execution = StrategyExecution.from_scan_result(scan, stake=1.0, currency="USD", duration=1)
    execution.begin_signal(1)
    s = execution.status("running")
    assert s["contract_type"] == "DIGITMATCH"
    assert s["barrier"] == 7


def test_from_scan_result_digitodd_omits_barrier_in_proposal():
    """DIGITODD proposal must not include a barrier field."""
    from app.strategy.market_scanner import ScanResult, ContractSpec
    spec = ContractSpec("1HZ50V", "DIGITODD", None, "odd")
    scan = ScanResult(spec, 72.0, 5, 12.1, "HIGH", True, False, "Digit 5 at 12.1%", 0.0)
    execution = StrategyExecution.from_scan_result(scan, stake=1.0, currency="USD", duration=1)
    proposal = execution._proposal(1)
    assert proposal["contract_type"] == "DIGITODD"
    assert "barrier" not in proposal


def test_rise_fall_proposal_uses_one_tick_without_barrier():
    execution = StrategyExecution(
        "R_100", Group.OVER, barrier=0, duration=1, duration_unit="t",
        contract_type_override="CALL",
    )
    proposal = execution._proposal(1)
    assert proposal["contract_type"] == "CALL"
    assert proposal["duration"] == 1
    assert proposal["duration_unit"] == "t"
    assert "barrier" not in proposal


def test_execution_accepts_frontend_configured_trade_count():
    execution = StrategyExecution("R_100", Group.OVER, barrier=3, max_buys=7)
    assert execution.max_buys == 7
    status = execution.status("configured")
    assert status["max_buys"] == 7
