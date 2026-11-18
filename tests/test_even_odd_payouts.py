import asyncio
import json

import pytest

from app.even_odd.payouts import (
    PayoutQuoteError,
    get_even_odd_payout_quote,
)


class FakeWebSocket:
    def __init__(self, *, error=None, bad_echo=False, invalid_payout=False, timeout=False):
        self.requests = []
        self.responses = []
        self.error = error
        self.bad_echo = bad_echo
        self.invalid_payout = invalid_payout
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, raw):
        request = json.loads(raw)
        self.requests.append(request)
        if self.error:
            self.responses.append({
                "msg_type": "proposal",
                "req_id": request["req_id"],
                "error": {"message": self.error},
            })
            return

        contract_type = request["contract_type"]
        ask_price = request["amount"]
        payout = ask_price if self.invalid_payout else (
            ask_price + (0.8 if contract_type == "DIGITEVEN" else 0.9)
        )
        echo = dict(request)
        if self.bad_echo:
            echo["underlying_symbol"] = "R_100"
        self.responses.append({
            "msg_type": "proposal",
            "req_id": request["req_id"],
            "echo_req": echo,
            "proposal": {
                "id": f"{contract_type.lower()}-proposal",
                "ask_price": ask_price,
                "payout": payout,
                "spot": 100.0,
                "spot_time": 1_800_000_000,
            },
        })

    async def recv(self):
        if self.timeout:
            await asyncio.sleep(1)
        if not self.responses:
            raise asyncio.TimeoutError
        return json.dumps(self.responses.pop())


def install_fake_websocket(monkeypatch, **kwargs):
    fake = FakeWebSocket(**kwargs)
    monkeypatch.setattr(
        "app.even_odd.payouts.websockets.connect",
        lambda *args, **options: fake,
    )
    return fake


@pytest.mark.asyncio
async def test_payout_quotes_use_deriv_digit_contracts_and_calculate_net_profit(monkeypatch):
    fake = install_fake_websocket(monkeypatch)

    result = await get_even_odd_payout_quote(
        symbol="R_50",
        currency="USD",
        stake="1.00",
    )

    assert len(fake.requests) == 2
    assert {item["contract_type"] for item in fake.requests} == {"DIGITEVEN", "DIGITODD"}
    assert all(item["duration"] == 1 and item["duration_unit"] == "t" for item in fake.requests)
    assert all(item["basis"] == "stake" and item["underlying_symbol"] == "R_50" for item in fake.requests)
    assert result["source"] == "deriv_public_websocket"
    assert result["even"]["ask_price"] == "1.0"
    assert result["even"]["payout"] == "1.8"
    assert result["even"]["profit_if_win"] == "0.8"
    assert result["even"]["loss_if_lose"] == "1.0"
    assert result["odd"]["profit_if_win"] == "0.9"
    assert result["execution_authorized"] is False


@pytest.mark.asyncio
async def test_payout_quote_rejects_deriv_errors_and_mismatched_echo(monkeypatch):
    install_fake_websocket(monkeypatch, error="Contract is unavailable")
    with pytest.raises(PayoutQuoteError, match="Contract is unavailable"):
        await get_even_odd_payout_quote(symbol="R_50", currency="USD", stake="1")

    install_fake_websocket(monkeypatch, bad_echo=True)
    with pytest.raises(PayoutQuoteError, match="does not match"):
        await get_even_odd_payout_quote(symbol="R_50", currency="USD", stake="1")


@pytest.mark.asyncio
async def test_payout_quote_rejects_non_positive_net_profit(monkeypatch):
    install_fake_websocket(monkeypatch, invalid_payout=True)
    with pytest.raises(PayoutQuoteError, match="no positive net profit"):
        await get_even_odd_payout_quote(symbol="R_50", currency="USD", stake="1")


@pytest.mark.asyncio
async def test_payout_quote_rejects_invalid_inputs_before_connecting(monkeypatch):
    def unexpected_connect(*args, **kwargs):
        raise AssertionError("WebSocket must not be opened for invalid inputs.")

    monkeypatch.setattr("app.even_odd.payouts.websockets.connect", unexpected_connect)
    with pytest.raises(ValueError, match="symbol"):
        await get_even_odd_payout_quote(symbol="R 50", currency="USD", stake="1")
    with pytest.raises(ValueError, match="Currency"):
        await get_even_odd_payout_quote(symbol="R_50", currency="usd", stake="1")
    with pytest.raises(ValueError, match="Stake"):
        await get_even_odd_payout_quote(symbol="R_50", currency="USD", stake="0")


@pytest.mark.asyncio
async def test_payout_quote_times_out_when_both_sides_do_not_arrive(monkeypatch):
    install_fake_websocket(monkeypatch, timeout=True)
    with pytest.raises(PayoutQuoteError, match="Timed out"):
        await get_even_odd_payout_quote(
            symbol="R_50",
            currency="USD",
            stake="1",
            timeout_seconds=0.01,
        )
