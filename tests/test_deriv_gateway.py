import time

from fastapi.testclient import TestClient

from app import deriv_gateway, main

client = TestClient(main.app)


def test_deriv_session_requires_app_authentication():
    response = client.get("/deriv/session")
    assert response.status_code == 401


def test_credential_transfer_fails_closed_without_bridge_secret(monkeypatch):
    monkeypatch.delenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", raising=False)
    response = client.post(
        "/deriv/internal/credentials",
        headers={"X-Tradebridge-Bridge-Token": "anything"},
        json={"user_id": "user-1", "access_token": "secret-token"},
    )
    assert response.status_code == 503
    assert "secret-token" not in response.text


def test_credential_transfer_requires_bridge_secret(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "expected-secret")
    response = client.post(
        "/deriv/internal/credentials",
        headers={"X-Tradebridge-Bridge-Token": "wrong-secret"},
        json={"user_id": "user-1", "access_token": "secret-token"},
    )
    assert response.status_code == 401
    assert "secret-token" not in response.text


def test_credential_transfer_never_returns_deriv_token(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "expected-secret")
    deriv_gateway._credentials.clear()
    response = client.post(
        "/deriv/internal/credentials",
        headers={"X-Tradebridge-Bridge-Token": "expected-secret"},
        json={"user_id": "user-1", "access_token": "secret-token", "expires_in": 3600},
    )
    assert response.status_code == 204
    assert "secret-token" not in response.text
    assert deriv_gateway._credentials["user-1"].access_token == "secret-token"
    deriv_gateway._credentials.clear()


def test_session_status_never_returns_credential(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "expected-secret")
    deriv_gateway._credentials.clear()
    transfer = client.post(
        "/deriv/internal/credentials",
        headers={"X-Tradebridge-Bridge-Token": "expected-secret"},
        json={"user_id": "user-1", "access_token": "secret-token", "expires_in": 3600},
    )
    assert transfer.status_code == 204
    main.app.dependency_overrides[deriv_gateway.current_user] = lambda: "user-1"
    try:
        response = client.get("/deriv/session")
    finally:
        main.app.dependency_overrides.pop(deriv_gateway.current_user, None)
        deriv_gateway._credentials.clear()
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert "secret-token" not in response.text
    assert "access_token" not in response.json()


def test_expired_credentials_are_removed():
    deriv_gateway._credentials["expired-user"] = deriv_gateway.Credential(
        access_token="secret-token",
        expires_at=time.time() - 1,
        updated_at=time.time() - 10,
    )
    main.app.dependency_overrides[deriv_gateway.current_user] = lambda: "expired-user"
    try:
        response = client.post("/deriv/accounts")
    finally:
        main.app.dependency_overrides.pop(deriv_gateway.current_user, None)
    assert response.status_code == 401
    assert "expired-user" not in deriv_gateway._credentials


def test_websocket_ticket_is_short_lived_and_one_time(monkeypatch):
    deriv_gateway._ws_tickets.clear()
    main.app.dependency_overrides[deriv_gateway.current_user] = lambda: "user-1"
    try:
        response = client.post("/deriv/ws-ticket")
    finally:
        main.app.dependency_overrides.pop(deriv_gateway.current_user, None)
    assert response.status_code == 200
    payload = response.json()
    assert payload["expires_in"] == 30
    ticket = payload["ticket"]
    assert deriv_gateway._ws_tickets[ticket][0] == "user-1"
    assert deriv_gateway._ws_tickets[ticket][1] > time.time()
    deriv_gateway._ws_tickets.clear()


def test_origin_allowlist_is_exact(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_ALLOWED_ORIGINS", "https://swigtrade.vercel.app, http://localhost:3000")
    assert deriv_gateway._origin_allowed("https://swigtrade.vercel.app")
    assert deriv_gateway._origin_allowed("http://localhost:3000")
    assert not deriv_gateway._origin_allowed("https://evil.example")
