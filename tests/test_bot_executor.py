"""Tests for the inline bot executor (app/bot_executor.py).

Uses a MockUpstream that pre-queues Deriv responses so run_bot() can run
synchronously in an asyncio.run() context without a real WebSocket connection.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.bot_executor import BotResult, run_bot
from app.bot_schema import DemoBotConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> DemoBotConfig:
    base = {
        "id": "bot-1",
        "name": "Test",
        "symbol": "1HZ100V",
        "contractType": "DIGITEVEN",
        "barrier": None,
        "duration": 1,
        "durationUnit": "t",
        "stakeStrategy": {"type": "fixed", "amount": 1.0},
        "stopConditions": [{"type": "tradeCount", "max": 5}],
        "cooldownSeconds": 1,
        "maxTradesPerDay": 20,
        "maxConsecutiveLosses": 3,
    }
    base.update(overrides)
    return DemoBotConfig.model_validate(base)


class MockUpstream:
    """Fake Deriv WebSocket driven by a response queue.

    Each time the executor sends a *proposal* or *buy* request, the mock makes
    the corresponding pre-configured response available for the reader task.
    Subscribe requests (proposal_open_contract) and other sends do NOT consume
    a response slot — their settlement message is queued when the buy response
    for that trade is sent (i.e. after buy ack, queue the settlement too).

    Response sequence layout (per trade):
      index 0: proposal response  (queued when proposal send arrives)
      index 1: buy response       (queued when buy send arrives)
      index 2: settlement msg     (queued together with buy response)
    """

    def __init__(self, response_sequence: list[dict]):
        self._responses = [json.dumps(r) for r in response_sequence]
        self._response_idx = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            msg = {}

        if "proposal" in msg and self._response_idx < len(self._responses):
            # Proposal send → queue the proposal response.
            await self._queue.put(self._responses[self._response_idx])
            self._response_idx += 1

        elif "buy" in msg:
            # Buy send → queue only the buy ACK.
            # The settlement is queued when the subscribe send arrives.
            await self._queue.put(self._responses[self._response_idx])
            self._response_idx += 1

        elif "proposal_open_contract" in msg:
            # Subscribe send → now the contract_future is registered; queue settlement.
            if self._response_idx < len(self._responses):
                await self._queue.put(self._responses[self._response_idx])
                self._response_idx += 1

        # Yield so the reader task can process what was just queued.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Use a generous timeout to survive cooldown periods between trades.
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=10.0)
            return item
        except asyncio.TimeoutError:
            raise StopAsyncIteration


def _proposal_response(req_id: int, proposal_id: str = "prop-1", ask_price: float = 1.0) -> dict:
    return {
        "msg_type": "proposal",
        "req_id": req_id,
        "proposal": {"id": proposal_id, "ask_price": ask_price},
    }


def _buy_response(req_id: int, contract_id: str = "12345") -> dict:
    return {
        "msg_type": "buy",
        "req_id": req_id,
        "buy": {"contract_id": contract_id, "buy_price": 1.0},
    }


def _settlement_response(contract_id: str = "12345", sell_price: float = 1.85, status: str = "won") -> dict:
    return {
        "msg_type": "proposal_open_contract",
        "proposal_open_contract": {
            "contract_id": contract_id,
            "sell_price": sell_price,
            "is_sold": True,
            "status": status,
        },
    }


def _one_trade_responses(
    proposal_req_id: int = 1,
    buy_req_id: int = 2,
    sub_req_id: int = 3,
    contract_id: str = "12345",
    sell_price: float = 1.85,
    status: str = "won",
) -> list[dict]:
    return [
        _proposal_response(proposal_req_id, ask_price=1.0),
        _buy_response(buy_req_id, contract_id=contract_id),
        _settlement_response(contract_id, sell_price=sell_price, status=status),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_proposal_uses_verified_account_currency():
    """The proposal currency must follow the verified account, not a hardcoded USD."""
    config = _make_config(stopConditions=[{"type": "tradeCount", "max": 1}])
    upstream = MockUpstream(_one_trade_responses())
    events: list[dict] = []

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, asyncio.Event(), currency="EUR"))

    proposal = next(json.loads(raw) for raw in upstream.sent if "proposal" in json.loads(raw))
    assert proposal["currency"] == "EUR"
    assert result.trade_count == 1


def test_single_trade_full_cycle():
    """A single winning trade emits the correct events and returns BotResult."""
    config = _make_config(stopConditions=[{"type": "tradeCount", "max": 1}])
    upstream = MockUpstream(_one_trade_responses())
    events: list[dict] = []

    async def send_event(e: dict) -> None:
        events.append(e)

    stop = asyncio.Event()
    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.trade_count == 1
    assert result.stop_reason == "trade_count"
    assert result.total_profit == pytest.approx(0.85, abs=1e-6)

    event_types = [e["event"] for e in events]
    assert "started" in event_types
    assert "trade_opened" in event_types
    assert "trade_settled" in event_types
    assert "done" not in event_types  # done is sent by bot_runner, not run_bot


def test_stop_signal_during_cooldown_exits_cleanly():
    """Setting stop_signal during cooldown exits after the current trade, not mid-trade."""
    config = _make_config(
        cooldownSeconds=10,
        stopConditions=[{"type": "tradeCount", "max": 5}],
    )
    # Provide responses for 2 trades; stop signal fires after the first settles.
    responses = _one_trade_responses(1, 2, 3) + _one_trade_responses(4, 5, 6, contract_id="99999")
    upstream = MockUpstream(responses)
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)
        # Set stop signal right after the first trade_settled event.
        if e.get("event") == "trade_settled" and e.get("trade_count") == 1:
            stop.set()

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.stop_reason == "user_requested"
    assert result.trade_count == 1  # stopped after first trade, during cooldown


def test_trade_count_stop_condition():
    """Bot stops exactly when tradeCount max is reached."""
    config = _make_config(stopConditions=[{"type": "tradeCount", "max": 2}], cooldownSeconds=1)
    # Provide 3 trades worth of responses; only 2 should execute.
    responses = (
        _one_trade_responses(1, 2, 3)
        + _one_trade_responses(4, 5, 6, contract_id="22222")
        + _one_trade_responses(7, 8, 9, contract_id="33333")
    )
    upstream = MockUpstream(responses)
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.trade_count == 2
    assert result.stop_reason == "trade_count"


def test_loss_limit_stop_condition():
    """Bot stops when cumulative loss exceeds the configured loss limit."""
    config = _make_config(
        stopConditions=[
            {"type": "loss", "limit": 0.5},
            {"type": "tradeCount", "max": 10},
        ],
        cooldownSeconds=1,
    )
    # One losing trade: stake=1.0, payout=0.0 → profit=-1.0, exceeds limit of 0.5.
    responses = _one_trade_responses(1, 2, 3, sell_price=0.0, status="lost")
    upstream = MockUpstream(responses)
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.stop_reason == "loss_limit"
    assert result.total_profit == pytest.approx(-1.0, abs=1e-6)



def test_losing_contract_does_not_use_potential_payout_as_actual_return():
    """A zero sell price must not fall back to a potentially positive payout."""
    config = _make_config(
        stopConditions=[
            {"type": "loss", "limit": 0.5},
            {"type": "tradeCount", "max": 10},
        ],
        cooldownSeconds=1,
    )
    responses = _one_trade_responses(1, 2, 3, sell_price=0.0, status="lost")
    # Simulate a provider payload that also contains a positive payout field.
    responses[-1]["proposal_open_contract"]["payout"] = 46.13
    upstream = MockUpstream(responses)
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.stop_reason == "loss_limit"
    assert result.total_profit == pytest.approx(-1.0, abs=1e-6)
    settled = next(event for event in events if event["event"] == "trade_settled")
    assert settled["payout"] == 0.0
    assert settled["profit"] == pytest.approx(-1.0, abs=1e-6)
    assert settled["outcome"] == "lost"

def test_consecutive_loss_stop():
    """Bot stops when maxConsecutiveLosses is reached."""
    config = _make_config(
        maxConsecutiveLosses=2,
        stopConditions=[{"type": "tradeCount", "max": 10}],
        cooldownSeconds=1,
    )
    # Two consecutive losing trades.
    responses = (
        _one_trade_responses(1, 2, 3, sell_price=0.0, status="lost")
        + _one_trade_responses(4, 5, 6, contract_id="22222", sell_price=0.0, status="lost")
    )
    upstream = MockUpstream(responses)
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.stop_reason == "max_consecutive_losses"
    assert result.trade_count == 2


def test_profit_target_stop_condition():
    """Bot stops when cumulative profit hits the profit target."""
    config = _make_config(
        stopConditions=[
            {"type": "profit", "target": 0.5},
            {"type": "tradeCount", "max": 10},
        ],
        cooldownSeconds=1,
    )
    # One winning trade: stake=1.0, payout=1.85 → profit=0.85 > target=0.5.
    responses = _one_trade_responses(1, 2, 3, sell_price=1.85)
    upstream = MockUpstream(responses)
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.stop_reason == "profit_target"


def test_proposal_error_transitions_to_failed():
    """A Deriv error on the proposal response stops the bot with stop_reason=proposal_error."""
    config = _make_config(stopConditions=[{"type": "tradeCount", "max": 5}])
    upstream = MockUpstream([
        {"msg_type": "proposal", "req_id": 1, "error": {"message": "Market closed"}},
    ])
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.stop_reason == "proposal_error"
    assert result.trade_count == 0
    error_events = [e for e in events if e.get("event") == "error"]
    assert any(e.get("reason") == "proposal_error" for e in error_events)


def test_buy_error_transitions_to_failed():
    """A Deriv error on the buy response stops the bot with stop_reason=buy_error."""
    config = _make_config(stopConditions=[{"type": "tradeCount", "max": 5}])
    upstream = MockUpstream([
        _proposal_response(1),
        {"msg_type": "buy", "req_id": 2, "error": {"message": "Insufficient balance"}},
    ])
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    assert result.stop_reason == "buy_error"
    assert result.trade_count == 0


def test_connection_closed_mid_proposal_stops_bot():
    """Connection dropping during proposal wait stops the bot cleanly."""
    config = _make_config(stopConditions=[{"type": "tradeCount", "max": 5}])
    # No responses at all — upstream closes immediately.
    upstream = MockUpstream([])
    events: list[dict] = []
    stop = asyncio.Event()

    async def send_event(e: dict) -> None:
        events.append(e)

    result = asyncio.run(run_bot(config, upstream, send_event, stop))

    # Either a timeout or connection_closed, depending on timing — just confirm it stopped.
    assert result.trade_count == 0
    assert result.stop_reason in ("proposal_timeout", "connection_closed")
