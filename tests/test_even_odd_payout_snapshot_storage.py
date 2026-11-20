import pytest

from app.even_odd.storage import (
    load_payout_quote_snapshots,
    save_payout_quote_snapshot,
)


@pytest.mark.asyncio
async def test_save_payout_snapshot_persists_only_matched_public_quote_terms(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

    class Client:
        calls = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, params, headers, json):
            self.calls.append((url, params, headers, json))
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", Client)
    quote = {
        "source": "deriv_public_websocket",
        "symbol": "R_50",
        "currency": "USD",
        "requested_stake": "1.00",
        "duration": 1,
        "duration_unit": "t",
        "quoted_at": "2026-11-20T00:00:00+00:00",
        "even": {"contract_type": "DIGITEVEN", "ask_price": "1.00", "payout": "1.95", "spot_time": 1795132800},
        "odd": {"contract_type": "DIGITODD", "ask_price": "1.00", "payout": "1.95", "spot_time": 1795132800},
    }

    await save_payout_quote_snapshot(quote=quote)

    url, params, headers, payload = Client.calls[0]
    assert url.endswith("/even_odd_payout_quote_snapshots")
    assert params["on_conflict"] == "market_symbol,currency,stake,spot_time"
    assert headers["Prefer"] == "resolution=ignore-duplicates,return=minimal"
    assert payload["market_symbol"] == "R_50"
    assert payload["even_ask_price"] == "1.00"
    assert payload["odd_payout"] == "1.95"
    assert payload["spot_time"] == 1795132800


@pytest.mark.asyncio
async def test_load_payout_snapshots_filters_exact_contract_and_time_range(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"spot_time": 1795132800}]

    class Client:
        last_call = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, params, headers):
            Client.last_call = (url, params, headers)
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", Client)

    result = await load_payout_quote_snapshots(
        symbol="R_50", currency="USD", stake="1.00",
        from_epoch=1795130000, to_epoch=1795139999,
    )

    assert result == [{"spot_time": 1795132800}]
    params = Client.last_call[1]
    assert params["market_symbol"] == "eq.R_50"
    assert params["currency"] == "eq.USD"
    assert params["stake"] == "eq.1.00"
    assert params["and"] == "(spot_time.gte.1795130000,spot_time.lte.1795139999)"
