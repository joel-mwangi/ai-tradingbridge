import time

from fastapi.testclient import TestClient
from app import ctrader_gateway, main

client = TestClient(main.app)


def test_quote_returns_account_specific_bid_ask(monkeypatch):
    user_id = "c7362ec4-1111-4111-8111-111111111111"

    async def credential(_user_id):
        return ctrader_gateway.CTraderCredential("access", "refresh", time.time() + 3600, 1, time.time())

    async def links(_user_id):
        return [{"account_id": "123456", "account_type": "demo"}]

    async def api(_cred, _account, _live, request_type, _payload, _expected, timeout_seconds=10.0):
        if request_type == ctrader_gateway.SYMBOLS_LIST_REQ:
            return ctrader_gateway.SYMBOLS_LIST_RES, {"symbol": [{"symbolId": 7, "symbolName": "EURUSD", "enabled": True}]}
        return ctrader_gateway.SYMBOL_BY_ID_RES, {"symbol": [{"symbolId": 7, "digits": 5, "pipPosition": 4}]}

    async def spot(_cred, _account, _live, _symbol):
        return {"bid_raw": 123450, "ask_raw": 123460, "provider_timestamp": str(int(time.time() * 1000))}

    monkeypatch.setattr(ctrader_gateway, "_owned_credential", credential)
    monkeypatch.setattr(ctrader_gateway, "_load_account_links", links)
    monkeypatch.setattr(ctrader_gateway, "_call_account_api", api)
    monkeypatch.setattr(ctrader_gateway, "_fetch_spot_quote", spot)
    main.app.dependency_overrides[ctrader_gateway.current_user] = lambda: user_id
    try:
        response = client.get("/ctrader/quote?account_id=123456&symbol_id=7")
    finally:
        main.app.dependency_overrides.pop(ctrader_gateway.current_user, None)

    assert response.status_code == 200
    body = response.json()
    assert body["bid"] == 1.2345
    assert body["ask"] == 1.2346
    assert body["spread_pips"] == 1.0
    assert body["quote_age_seconds"] <= 15


def test_quote_rejects_stale_spot_snapshot(monkeypatch):
    user_id = "c7362ec4-1111-4111-8111-111111111111"

    async def credential(_user_id):
        return ctrader_gateway.CTraderCredential("access", "refresh", time.time() + 3600, 1, time.time())

    async def links(_user_id):
        return [{"account_id": "123456", "account_type": "demo"}]

    async def api(_cred, _account, _live, request_type, _payload, _expected, timeout_seconds=10.0):
        if request_type == ctrader_gateway.SYMBOLS_LIST_REQ:
            return ctrader_gateway.SYMBOLS_LIST_RES, {"symbol": [{"symbolId": 7, "symbolName": "EURUSD", "enabled": True}]}
        return ctrader_gateway.SYMBOL_BY_ID_RES, {"symbol": [{"symbolId": 7, "digits": 5, "pipPosition": 4}]}

    async def stale_spot(_cred, _account, _live, _symbol):
        return {"bid_raw": 123450, "ask_raw": 123460, "provider_timestamp": int((time.time() - 60) * 1000)}

    monkeypatch.setattr(ctrader_gateway, "_owned_credential", credential)
    monkeypatch.setattr(ctrader_gateway, "_load_account_links", links)
    monkeypatch.setattr(ctrader_gateway, "_call_account_api", api)
    monkeypatch.setattr(ctrader_gateway, "_fetch_spot_quote", stale_spot)
    main.app.dependency_overrides[ctrader_gateway.current_user] = lambda: user_id
    try:
        response = client.get("/ctrader/quote?account_id=123456&symbol_id=7")
    finally:
        main.app.dependency_overrides.pop(ctrader_gateway.current_user, None)

    assert response.status_code == 409
    assert "stale" in response.json()["detail"].lower()
