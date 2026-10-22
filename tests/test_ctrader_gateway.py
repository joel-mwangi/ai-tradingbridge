import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import ctrader_gateway, main

client = TestClient(main.app)


def test_ctrader_session_requires_tradebridge_authentication():
    response = client.get("/ctrader/session")
    assert response.status_code == 401


def test_ctrader_oauth_exchange_fails_closed_without_bridge_secret(monkeypatch):
    monkeypatch.delenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", raising=False)
    response = client.post(
        "/ctrader/internal/oauth/exchange",
        json={
            "user_id": "c7362ec4-1111-4111-8111-111111111111",
            "code": "authorization-code",
            "redirect_uri": "https://app.example/ctrader/callback",
        },
    )
    assert response.status_code == 503
    assert "authorization-code" not in response.text


def test_ctrader_oauth_rejects_redirect_mismatch_before_provider_call(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("CTRADER_OPENAPI_CLIENT_ID", "client-id")
    monkeypatch.setenv("CTRADER_OPENAPI_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("CTRADER_OAUTH_REDIRECT_URI", "https://app.example/api/auth/ctrader/callback")
    response = client.post(
        "/ctrader/internal/oauth/exchange",
        headers={"X-Tradebridge-Bridge-Token": "bridge-secret"},
        json={
            "user_id": "c7362ec4-1111-4111-8111-111111111111",
            "code": "authorization-code",
            "redirect_uri": "https://attacker.example/api/auth/ctrader/callback",
        },
    )
    assert response.status_code == 400
    assert "authorization-code" not in response.text


def test_forex_symbol_filter_rejects_non_fx_assets():
    assert ctrader_gateway._is_forex_symbol("EURUSD")
    assert ctrader_gateway._is_forex_symbol("EUR/USD")
    assert ctrader_gateway._is_forex_symbol("USDJPY")
    assert not ctrader_gateway._is_forex_symbol("XAUUSD")
    assert not ctrader_gateway._is_forex_symbol("BTCUSD")
    assert not ctrader_gateway._is_forex_symbol("EUR")
    assert not ctrader_gateway._is_forex_symbol("EURUSDEUR")


def test_order_request_rejects_bad_idempotency_keys():
    with pytest.raises(ValueError):
        ctrader_gateway.CTraderOrderRequest(
            account_id="123456",
            symbol_id=1,
            side="BUY",
            volume_units=1000,
            idempotency_key="short",
            confirmed=True,
        )


def test_order_request_rejects_non_finite_volume():
    with pytest.raises(ValueError):
        ctrader_gateway.CTraderOrderRequest(
            account_id="123456",
            symbol_id=1,
            side="BUY",
            volume_units=float("nan"),
            idempotency_key="order-key-123",
            confirmed=True,
        )


def test_demo_order_execution_is_off_unless_operator_enables_it(monkeypatch):
    monkeypatch.setenv("CTRADER_DEMO_ORDERS_ENABLED", "false")
    main.app.dependency_overrides[ctrader_gateway.current_user] = lambda: "c7362ec4-1111-4111-8111-111111111111"
    try:
        response = client.post(
            "/ctrader/orders",
            json={
                "account_id": "123456",
                "symbol_id": 1,
                "side": "BUY",
                "volume_units": 1000,
                "idempotency_key": "one-time-demo-order-123",
                "confirmed": True,
            },
        )
    finally:
        main.app.dependency_overrides.pop(ctrader_gateway.current_user, None)
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"].lower()


def test_order_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setenv("CTRADER_DEMO_ORDERS_ENABLED", "true")
    main.app.dependency_overrides[ctrader_gateway.current_user] = lambda: "c7362ec4-1111-4111-8111-111111111111"
    try:
        response = client.post(
            "/ctrader/orders",
            json={
                "account_id": "123456",
                "symbol_id": 1,
                "side": "BUY",
                "volume_units": 1000,
                "idempotency_key": "one-time-demo-order-456",
                "confirmed": False,
            },
        )
    finally:
        main.app.dependency_overrides.pop(ctrader_gateway.current_user, None)
    assert response.status_code == 400
    assert "confirmation" in response.json()["detail"].lower()


def test_real_account_order_execution_is_always_locked(monkeypatch):
    monkeypatch.setenv("CTRADER_DEMO_ORDERS_ENABLED", "true")
    monkeypatch.setenv("CTRADER_DEMO_MAX_VOLUME_UNITS", "1000")

    async def fake_owned_credential(_user_id):
        return ctrader_gateway.CTraderCredential(
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
            permission_scope=1,
            updated_at=time.time(),
        )

    async def fake_account_links(_user_id):
        return [{
            "account_id": "123456",
            "account_type": "live",
            "trader_login": None,
            "broker_title_short": "Test broker",
        }]

    monkeypatch.setattr(ctrader_gateway, "_owned_credential", fake_owned_credential)
    monkeypatch.setattr(ctrader_gateway, "_load_account_links", fake_account_links)
    main.app.dependency_overrides[ctrader_gateway.current_user] = lambda: "c7362ec4-1111-4111-8111-111111111111"
    try:
        response = client.post(
            "/ctrader/orders",
            json={
                "account_id": "123456",
                "symbol_id": 1,
                "side": "BUY",
                "volume_units": 1000,
                "idempotency_key": "one-time-demo-order-789",
                "confirmed": True,
            },
        )
    finally:
        main.app.dependency_overrides.pop(ctrader_gateway.current_user, None)
    assert response.status_code == 403
    assert "real-account" in response.json()["detail"].lower()


def test_send_and_wait_correlates_response_to_request_id():
    class FakeWebSocket:
        sent = None

        async def send(self, raw):
            self.sent = json.loads(raw)

        async def recv(self):
            return json.dumps({
                "clientMsgId": self.sent["clientMsgId"],
                "payloadType": ctrader_gateway.APP_AUTH_RES,
                "payload": {},
            })

    websocket = FakeWebSocket()
    response_type, payload = asyncio.run(
        ctrader_gateway._send_and_wait(
            websocket,
            ctrader_gateway.APP_AUTH_REQ,
            {"clientId": "client", "clientSecret": "secret"},
            {ctrader_gateway.APP_AUTH_RES},
        )
    )
    assert websocket.sent["payloadType"] == ctrader_gateway.APP_AUTH_REQ
    assert response_type == ctrader_gateway.APP_AUTH_RES
    assert payload == {}


def test_json_websocket_endpoint_config():
    assert ctrader_gateway.DEMO_HOST == "demo.ctraderapi.com"
    assert ctrader_gateway.LIVE_HOST == "live.ctraderapi.com"
    assert ctrader_gateway.OPENAPI_PORT == 5036


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"executionType": 2}, 2),
        ({"executionType": "3"}, 3),
        ({"executionType": "ORDER_FILLED"}, 3),
        ({"executionType": 7}, 7),
        ({"executionType": "ORDER_PARTIAL_FILL"}, 11),
        ({"executionType": "unexpected"}, None),
        ({}, None),
    ],
)
def test_execution_type_normalization(payload, expected):
    assert ctrader_gateway._execution_type_value(payload) == expected
