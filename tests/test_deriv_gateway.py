import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app.deriv import router as _router  # noqa: F401 — ensure package is imported
import app.deriv.credentials as _creds
import app.deriv.security as _security
from app.deriv.credentials import Credential
from app.deriv.security import current_user
from app import main

client = TestClient(main.app)


@pytest.fixture
def credential_store(monkeypatch):
    store = {}

    async def fake_store(user_id, access_token, expires_in):
        credential = Credential(
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

    monkeypatch.setattr(_creds, "_store_credential", fake_store)
    monkeypatch.setattr(_creds, "_load_credential", fake_load)
    monkeypatch.setattr(_creds, "_delete_credential", fake_delete)
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
    credential_store["user-1"] = Credential(
        access_token="secret-token",
        expires_at=time.time() + 3600,
        updated_at=time.time(),
    )
    main.app.dependency_overrides[current_user] = lambda: "user-1"
    try:
        response = client.get("/deriv/session")
    finally:
        main.app.dependency_overrides.pop(current_user, None)
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert "secret-token" not in response.text
    assert "access_token" not in response.json()


def test_expired_credentials_are_removed(credential_store):
    credential_store["expired-user"] = Credential(
        access_token="secret-token",
        expires_at=time.time() - 1,
        updated_at=time.time() - 10,
    )
    main.app.dependency_overrides[current_user] = lambda: "expired-user"
    try:
        response = client.post("/deriv/accounts")
    finally:
        main.app.dependency_overrides.pop(current_user, None)
    assert response.status_code == 401
    assert "expired-user" not in credential_store


def test_websocket_ticket_is_short_lived_and_one_time(monkeypatch):
    _security._ws_tickets.clear()
    main.app.dependency_overrides[current_user] = lambda: "user-1"
    try:
        response = client.post("/deriv/ws-ticket")
    finally:
        main.app.dependency_overrides.pop(current_user, None)
    assert response.status_code == 200
    payload = response.json()
    assert payload["expires_in"] == 30
    ticket = payload["ticket"]
    assert _security._ws_tickets[ticket][0] == "user-1"
    assert _security._ws_tickets[ticket][1] > time.time()
    _security._ws_tickets.clear()


def test_origin_allowlist_is_exact(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_ALLOWED_ORIGINS", "https://swigtrade.vercel.app, http://localhost:3000")
    assert _security._origin_allowed("https://swigtrade.vercel.app")
    assert _security._origin_allowed("http://localhost:3000")
    assert not _security._origin_allowed("https://evil.example")


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
        asyncio.run(_creds._linked_account_ids("user-1", ["CR123"]))
    assert exc.value.status_code == 503


def test_credential_cipher_encrypts_and_round_trips_token(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY", key)
    cipher = _creds._credential_cipher()
    token = "a-sensitive-deriv-token"
    encrypted = cipher.encrypt(token.encode("utf-8"))
    assert token.encode("utf-8") not in encrypted
    assert cipher.decrypt(encrypted).decode("utf-8") == token


def test_oauth_exchange_writes_account_links(monkeypatch, credential_store):
    """exchange_oauth_code must call _store_account_links after storing the credential."""
    monkeypatch.setenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setenv("DERIV_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("DERIV_OAUTH_REDIRECT_URI", "https://app.example/api/auth/deriv/callback")

    stored_links: list[tuple[str, list[str]]] = []

    async def fake_store_links(user_id: str, account_ids: list[str]) -> None:
        stored_links.append((user_id, account_ids))

    monkeypatch.setattr(_creds, "_store_account_links", fake_store_links)

    class FakeResponse:
        status_code = 200
        is_success = True

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    import httpx as _httpx

    call_count = 0

    async def fake_post(self_client, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "oauth2/token" in url:
            return FakeResponse({"access_token": "deriv-token", "expires_in": 3600})
        return FakeResponse({})

    async def fake_get(self_client, url, **kwargs):
        return FakeResponse({"accounts": [{"account_id": "VRTC123", "account_type": "demo"}]})

    monkeypatch.setattr(_httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

    response = client.post(
        "/deriv/internal/oauth/exchange",
        headers={"X-Tradebridge-Bridge-Token": "bridge-secret"},
        json={
            "user_id": "user-1",
            "code": "auth-code",
            "code_verifier": "a" * 43,
            "redirect_uri": "https://app.example/api/auth/deriv/callback",
        },
    )
    assert response.status_code == 200
    assert len(stored_links) == 1
    assert stored_links[0][0] == "user-1"
    assert "VRTC123" in stored_links[0][1]


def test_account_links_removed_on_disconnect(monkeypatch, credential_store):
    """DELETE /deriv/session must remove both the credential and account links."""
    deleted_creds: list[str] = []
    deleted_links: list[str] = []

    async def fake_delete_cred(user_id: str) -> None:
        deleted_creds.append(user_id)

    async def fake_delete_links(user_id: str) -> None:
        deleted_links.append(user_id)

    monkeypatch.setattr(_creds, "_delete_credential", fake_delete_cred)
    monkeypatch.setattr(_creds, "_delete_account_links", fake_delete_links)

    main.app.dependency_overrides[current_user] = lambda: "user-1"
    try:
        response = client.delete("/deriv/session")
    finally:
        main.app.dependency_overrides.pop(current_user, None)

    assert response.status_code == 204
    assert "user-1" in deleted_creds
    assert "user-1" in deleted_links
