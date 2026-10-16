import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app import deriv_gateway, main

client = TestClient(main.app)


@pytest.fixture
def credential_store(monkeypatch):
    store = {}

    async def fake_store(user_id, access_token, expires_in):
        credential = deriv_gateway.Credential(
            access_token=access_token,
            expires_at=time.time() + expires_in,
            updated_at=time.time(),
        )
        store[user_id] = credential
        return credential

    async def fake_load(user_id):
        return store.get(user_id)

    async def fake_delete(user_id):
        store.pop(user_id, None)

    monkeypatch.setattr(deriv_gateway, "_store_credential", fake_store)
    monkeypatch.setattr(deriv_gateway, "_load_credential", fake_load)
    monkeypatch.setattr(deriv_gateway, "_delete_credential", fake_delete)
    return store


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


def test_credential_transfer_never_returns_deriv_token(monkeypatch, credential_store):
    monkeypatch.setenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "expected-secret")
    response = client.post(
        "/deriv/internal/credentials",
        headers={"X-Tradebridge-Bridge-Token": "expected-secret"},
        json={"user_id": "user-1", "access_token": "secret-token", "expires_in": 3600},
    )
    assert response.status_code == 204
    assert "secret-token" not in response.text
    assert credential_store["user-1"].access_token == "secret-token"


def test_session_status_never_returns_credential(monkeypatch, credential_store):
    credential_store["user-1"] = deriv_gateway.Credential(
        access_token="secret-token",
        expires_at=time.time() + 3600,
        updated_at=time.time(),
    )
    main.app.dependency_overrides[deriv_gateway.current_user] = lambda: "user-1"
    try:
        response = client.get("/deriv/session")
    finally:
        main.app.dependency_overrides.pop(deriv_gateway.current_user, None)
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert "secret-token" not in response.text
    assert "access_token" not in response.json()


def test_expired_credentials_are_removed(credential_store):
    credential_store["expired-user"] = deriv_gateway.Credential(
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
    assert "expired-user" not in credential_store


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


def test_oauth_exchange_fails_closed_without_bridge_secret(monkeypatch):
    monkeypatch.delenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", raising=False)
    response = client.post(
        "/deriv/internal/oauth/exchange",
        json={
            "user_id": "user-1",
            "code": "authorization-code",
            "code_verifier": "a" * 43,
            "redirect_uri": "https://app.example/api/auth/deriv/callback",
        },
    )
    assert response.status_code == 503
    assert "authorization-code" not in response.text


def test_oauth_exchange_rejects_redirect_uri_mismatch_before_provider_call(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "expected-secret")
    monkeypatch.setenv("DERIV_OAUTH_CLIENT_ID", "public-client-id")
    monkeypatch.setenv("DERIV_OAUTH_REDIRECT_URI", "https://app.example/api/auth/deriv/callback")
    response = client.post(
        "/deriv/internal/oauth/exchange",
        headers={"X-Tradebridge-Bridge-Token": "expected-secret"},
        json={
            "user_id": "user-1",
            "code": "authorization-code",
            "code_verifier": "a" * 43,
            "redirect_uri": "https://attacker.example/api/auth/deriv/callback",
        },
    )
    assert response.status_code == 400
    assert "authorization-code" not in response.text


def test_linked_account_lookup_fails_closed_without_service_role(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(deriv_gateway._linked_account_ids("user-1", ["CR123"]))
    assert exc.value.status_code == 503


def test_credential_cipher_encrypts_and_round_trips_token(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY", key)
    cipher = deriv_gateway._credential_cipher()
    token = "a-sensitive-deriv-token"
    encrypted = cipher.encrypt(token.encode("utf-8"))
    assert token.encode("utf-8") not in encrypted
    assert cipher.decrypt(encrypted).decode("utf-8") == token
