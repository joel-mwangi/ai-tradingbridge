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


async def verified_real_consent(user_id: str, account_id: str) -> dict:
    return {
        "enabled": True,
        "acknowledgement_version": "REAL_MONEY_RISK_ACKNOWLEDGEMENT_V1",
        "max_stake": 25,
        "max_daily_loss": 25,
    }


def set_fresh_real_profit(session, profit: float = 0.0) -> None:
    session.daily_realized_profit = profit
    session.daily_profit_checked_at = time.time()


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


@pytest.mark.anyio
async def test_strategy_second_cycle_does_not_duplicate_tick_subscription():
    import json
    from app.deriv.client_handler import _handle_strategy_start
    from app.deriv.trading_session import TradingProxySession

    session = TradingProxySession(allowed_client_requests={"ticks", "ticks_history"})
    upstream_messages = []
    ws_messages = []

    class FakeUpstream:
        async def send(self, msg: str):
            upstream_messages.append(json.loads(msg))

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            ws_messages.append(msg)

    control = {
        "action": "start",
        "symbol": "1HZ100V",
        "window_size": 1000,
        "barrier": 5,
        "direction": "over",
        "stake": 1,
        "duration": 1,
        "currency": "USD",
    }

    # Cycle 1 start
    await _handle_strategy_start(session, FakeUpstream(), FakeWebSocket(), control)
    assert any(m.get("ticks") == "1HZ100V" and m.get("subscribe") == 1 for m in upstream_messages)
    assert any("ticks_history" in m for m in upstream_messages)

    # Populate warm ticks in frequency window
    for i in range(10):
        session.strategy_frequency.add_tick(100.0 + i, 1000 + i, 2)
    assert len(session.strategy_frequency.points) == 10

    # Clear upstream messages recorded
    upstream_messages.clear()
    ws_messages.clear()

    # Cycle 2 start on the same symbol immediately
    await _handle_strategy_start(session, FakeUpstream(), FakeWebSocket(), control)

    # Must NOT re-send {"ticks": "1HZ100V", "subscribe": 1}
    assert not any(m.get("ticks") == "1HZ100V" and m.get("subscribe") == 1 for m in upstream_messages)
    # Warm frequency window must NOT have been wiped
    assert len(session.strategy_frequency.points) == 10

    # Switching to a different symbol forgets previous ticks and subscribes to new symbol
    upstream_messages.clear()
    control_symbol2 = dict(control, symbol="R_50")
    await _handle_strategy_start(session, FakeUpstream(), FakeWebSocket(), control_symbol2)
    assert any(m.get("forget_all") == "ticks" for m in upstream_messages)
    assert any(m.get("ticks") == "R_50" and m.get("subscribe") == 1 for m in upstream_messages)
    assert session.strategy_subscribed_symbol == "R_50"


def test_activate_account_session_supports_real_account(monkeypatch, credential_store):
    """activate_account_session must support real accounts with Deriv's ws/real endpoint."""
    credential_store["user-1"] = Credential(
        access_token="real-token",
        expires_at=time.time() + 3600,
        updated_at=time.time(),
    )
    main.app.dependency_overrides[current_user] = lambda: "user-1"

    async def fake_linked(user_id, account_ids):
        return set(account_ids)

    from app.deriv import routes as _routes
    monkeypatch.setattr(_routes, "_linked_account_ids", fake_linked)

    class FakeResponse:
        status_code = 200
        is_success = True

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    import httpx as _httpx

    async def fake_get(self_client, url, **kwargs):
        return FakeResponse({"accounts": [{"account_id": "CR123456", "account_type": "real"}]})

    async def fake_post(self_client, url, **kwargs):
        if "otp" in url:
            return FakeResponse({"data": {"url": "wss://api.derivws.com/trading/v1/options/ws/real?otp=test-otp"}})
        return FakeResponse({})

    monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(_httpx.AsyncClient, "post", fake_post)

    try:
        response = client.post("/deriv/session/activate", json={"account_id": "CR123456"})
    finally:
        main.app.dependency_overrides.pop(current_user, None)

    assert response.status_code == 200
    data = response.json()
    assert data["account_id"] == "CR123456"
    assert data["account_type"] == "real"
    assert data["status"] == "provider_session_verified"
    assert data["next_step"] == "connect_authenticated_websocket"


@pytest.mark.anyio
async def test_validate_proposal_real_stake_limit(monkeypatch):
    """Confirmed Real proposal validation respects TRADEBRIDGE_REAL_MAX_STAKE."""
    import app.deriv.client_handler as _client_handler
    from app.deriv.client_handler import _validate_proposal
    from app.deriv.trading_session import TradingProxySession

    session = TradingProxySession(
        allowed_client_requests={"proposal"}, account_type="real",
        user_id="user-1", account_id="CR123456",
    )
    sent = []

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            sent.append(msg)

    monkeypatch.setenv("TRADEBRIDGE_REAL_MAX_STAKE", "25")
    monkeypatch.setenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "true")
    monkeypatch.setattr(_client_handler, "_load_live_trading_consent", verified_real_consent)
    session.real_trading_confirmed = True
    set_fresh_real_profit(session)

    # Exceeds real limit
    res_exceeded = await _validate_proposal(session, FakeWebSocket(), {
        "amount": "50",
        "contract_type": "DIGITOVER",
        "duration": 5,
        "duration_unit": "t",
        "barrier": "5",
        "symbol": "1HZ100V",
        "req_id": 1,
    })
    assert res_exceeded is False
    assert any("Real stake is outside" in str(m) for m in sent)

    # Within real limit
    sent.clear()
    res_ok = await _validate_proposal(session, FakeWebSocket(), {
        "amount": "20",
        "contract_type": "DIGITOVER",
        "duration": 5,
        "duration_unit": "t",
        "barrier": "5",
        "symbol": "1HZ100V",
        "req_id": 2,
    })
    assert res_ok is True
    assert len(sent) == 0


@pytest.mark.anyio
async def test_real_proposal_is_blocked_by_default(monkeypatch):
    from app.deriv.client_handler import _validate_proposal
    from app.deriv.trading_session import TradingProxySession

    monkeypatch.delenv("TRADEBRIDGE_REAL_TRADING_ENABLED", raising=False)
    monkeypatch.setenv("TRADEBRIDGE_REAL_MAX_STAKE", "25")
    session = TradingProxySession(allowed_client_requests={"proposal"}, account_type="real")
    sent = []

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            sent.append(msg)

    accepted = await _validate_proposal(session, FakeWebSocket(), {
        "amount": 5,
        "contract_type": "DIGITOVER",
        "duration": 1,
        "duration_unit": "t",
        "barrier": 3,
        "symbol": "1HZ100V",
        "req_id": 1,
    })

    assert accepted is False
    assert sent[0]["error"]["code"] == "REAL_TRADING_DISABLED"


@pytest.mark.anyio
async def test_real_buy_is_blocked_without_connection_confirmation(monkeypatch):
    import app.deriv.client_handler as _client_handler
    from app.deriv.client_handler import _validate_buy
    from app.deriv.trading_session import TradingProxySession

    monkeypatch.setenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("TRADEBRIDGE_REAL_MAX_STAKE", "25")
    monkeypatch.setattr(_client_handler, "_load_live_trading_consent", verified_real_consent)
    session = TradingProxySession(
        allowed_client_requests={"buy"}, account_type="real",
        user_id="user-1", account_id="CR123456",
    )
    set_fresh_real_profit(session)
    session.valid_proposals["fresh-proposal"] = 5.0
    sent = []

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            sent.append(msg)

    accepted = await _validate_buy(session, FakeWebSocket(), {
        "buy": "fresh-proposal",
        "price": 5.0,
        "req_id": 2,
    })

    assert accepted is False
    assert sent[0]["error"]["code"] == "REAL_TRADING_CONFIRMATION_REQUIRED"
    assert "fresh-proposal" in session.valid_proposals


@pytest.mark.anyio
async def test_real_confirmation_requires_server_enablement_and_stake_limit(monkeypatch):
    import app.deriv.client_handler as _client_handler
    from app.deriv.client_handler import REAL_TRADING_CONFIRMATION, _handle_real_trading_control
    from app.deriv.trading_session import TradingProxySession

    session = TradingProxySession(
        allowed_client_requests=set(), account_type="real",
        user_id="user-1", account_id="CR123456",
    )
    monkeypatch.setattr(_client_handler, "_load_live_trading_consent", verified_real_consent)
    sent = []

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            sent.append(msg)

    control = {"action": "confirm", "confirmation": REAL_TRADING_CONFIRMATION}
    monkeypatch.setenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "true")
    monkeypatch.delenv("TRADEBRIDGE_REAL_MAX_STAKE", raising=False)
    await _handle_real_trading_control(session, FakeWebSocket(), control)
    assert sent[-1]["error"]["code"] == "REAL_STAKE_LIMIT_NOT_CONFIGURED"
    assert session.real_trading_confirmed is False

    monkeypatch.setenv("TRADEBRIDGE_REAL_MAX_STAKE", "10")
    await _handle_real_trading_control(
        session, FakeWebSocket(), {"action": "confirm", "confirmation": "yes"}
    )
    assert sent[-1]["error"]["code"] == "REAL_TRADING_CONFIRMATION_REQUIRED"
    assert session.real_trading_confirmed is False

    await _handle_real_trading_control(session, FakeWebSocket(), control)
    assert sent[-1]["type"] == "real_trading_status"
    assert sent[-1]["enabled"] is True
    assert session.real_trading_confirmed is True


@pytest.mark.anyio
async def test_real_strategy_start_requires_confirmation(monkeypatch):
    import json
    import app.deriv.client_handler as _client_handler
    from app.deriv.client_handler import _handle_strategy_start
    from app.deriv.trading_session import TradingProxySession

    monkeypatch.setenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("TRADEBRIDGE_REAL_MAX_STAKE", "10")
    monkeypatch.setattr(_client_handler, "_load_live_trading_consent", verified_real_consent)
    session = TradingProxySession(
        allowed_client_requests={"ticks", "ticks_history"}, account_type="real",
        user_id="user-1", account_id="CR123456",
    )
    set_fresh_real_profit(session)
    upstream_messages = []
    ws_messages = []

    class FakeUpstream:
        async def send(self, msg: str):
            upstream_messages.append(json.loads(msg))

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            ws_messages.append(msg)

    await _handle_strategy_start(session, FakeUpstream(), FakeWebSocket(), {
        "action": "start",
        "symbol": "1HZ100V",
        "window_size": 1000,
        "barrier": 3,
        "direction": "over",
        "stake": 1,
        "duration": 1,
        "currency": "USD",
    })

    assert ws_messages[0]["error"]["code"] == "REAL_TRADING_CONFIRMATION_REQUIRED"
    assert session.strategy_execution is None
    assert upstream_messages == []


@pytest.mark.anyio
async def test_multipliers_proposal_validates_product_fields():
    from app.deriv.client_handler import _validate_proposal
    from app.deriv.trading_session import TradingProxySession

    session = TradingProxySession(
        allowed_client_requests={"proposal"}, account_type="demo", product_api="multipliers"
    )
    sent = []

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            sent.append(msg)

    valid = {
        "proposal": 1,
        "amount": 10,
        "basis": "stake",
        "contract_type": "MULTUP",
        "currency": "USD",
        "symbol": "R_75",
        "multiplier": 20,
        "limit_order": {"take_profit": 10, "stop_loss": 5},
        "req_id": 11,
    }
    assert await _validate_proposal(session, FakeWebSocket(), valid) is True

    invalid = dict(valid, multiplier=20.5)
    assert await _validate_proposal(session, FakeWebSocket(), invalid) is False
    assert sent[-1]["error"]["code"] == "INVALID_MULTIPLIER"

    invalid = dict(valid, cancellation="30m")
    assert await _validate_proposal(session, FakeWebSocket(), invalid) is False
    assert sent[-1]["error"]["code"] == "INCOMPATIBLE_RISK_OPTIONS"


@pytest.mark.anyio
async def test_multipliers_real_proposal_requires_real_gate(monkeypatch):
    import app.deriv.client_handler as _client_handler
    from app.deriv.client_handler import _validate_proposal
    from app.deriv.trading_session import TradingProxySession

    monkeypatch.setenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("TRADEBRIDGE_REAL_MAX_STAKE", "25")
    session = TradingProxySession(
        allowed_client_requests={"proposal"}, account_type="real", product_api="multipliers",
        user_id="user-1", account_id="CR123456",
    )
    monkeypatch.setattr(_client_handler, "_load_live_trading_consent", verified_real_consent)
    session.real_trading_confirmed = True
    set_fresh_real_profit(session)

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            raise AssertionError(f"Unexpected validation error: {msg}")

    assert await _validate_proposal(session, FakeWebSocket(), {
        "proposal": 1,
        "amount": 10,
        "basis": "stake",
        "contract_type": "MULTDOWN",
        "currency": "USD",
        "symbol": "R_75",
        "multiplier": 50,
        "cancellation": "30m",
        "req_id": 12,
    }) is True


@pytest.mark.anyio
async def test_real_daily_loss_limit_blocks_proposals(monkeypatch):
    import app.deriv.client_handler as _client_handler
    from app.deriv.client_handler import _validate_proposal
    from app.deriv.trading_session import TradingProxySession

    monkeypatch.setenv("TRADEBRIDGE_REAL_TRADING_ENABLED", "true")
    monkeypatch.setenv("TRADEBRIDGE_REAL_MAX_STAKE", "25")
    monkeypatch.setattr(_client_handler, "_load_live_trading_consent", verified_real_consent)
    session = TradingProxySession(
        allowed_client_requests={"proposal"}, account_type="real",
        user_id="user-1", account_id="CR123456",
    )
    session.real_trading_confirmed = True
    set_fresh_real_profit(session, -25)
    sent = []

    class FakeWebSocket:
        async def send_json(self, msg: dict):
            sent.append(msg)

    accepted = await _validate_proposal(session, FakeWebSocket(), {
        "proposal": 1,
        "amount": 5,
        "basis": "stake",
        "contract_type": "MULTUP",
        "currency": "USD",
        "symbol": "R_75",
        "multiplier": 20,
        "req_id": 13,
    })
    assert accepted is False
    assert sent[0]["error"]["code"] == "REAL_DAILY_LOSS_LIMIT_REACHED"


def test_daily_profit_table_requires_correlated_complete_finite_values():
    from app.deriv.trading_session import TradingProxySession

    session = TradingProxySession(allowed_client_requests=set(), account_type="real")
    session.daily_profit_request_id = 45
    assert session.record_daily_profit_table(44, {"profit_table": {"transactions": []}}) is False
    assert session.daily_realized_profit is None

    assert session.record_daily_profit_table(45, {
        "profit_table": {"transactions": [{"profit": "4.5"}, {"profit": -2}]}
    }) is True
    assert session.daily_realized_profit == 2.5
    assert session.daily_profit_checked_at is not None

    session.daily_profit_request_id = 46
    assert session.record_daily_profit_table(46, {
        "profit_table": {"transactions": [{"profit": "NaN"}]}
    }) is True
    assert session.daily_realized_profit is None
    assert session.daily_profit_checked_at is None


def test_multipliers_authorization_must_match_selected_account():
    from app.deriv.multipliers_proxy import _authorized_login_matches

    assert _authorized_login_matches({"loginid": "CR123456"}, "CR123456") is True
    assert _authorized_login_matches({"loginid": "CR654321"}, "CR123456") is False
    assert _authorized_login_matches(None, "CR123456") is False


@pytest.mark.anyio
async def test_multipliers_websocket_authorizes_the_selected_account(monkeypatch):
    import json
    from app.deriv import multipliers_proxy

    account_id = "VRTC123"
    ticket = "multipliers-ticket"
    multipliers_proxy._ws_tickets[ticket] = ("user-1", time.time() + 30)
    monkeypatch.setenv("DERIV_APP_ID", "123456")

    class FakeCredential:
        access_token = "oauth-access-token"

    async def owned_credential(user_id):
        return FakeCredential()

    async def selected_account_type(user_id, selected_id, access_token):
        assert selected_id == account_id
        return "demo"

    monkeypatch.setattr(multipliers_proxy, "_owned_credential", owned_credential)
    monkeypatch.setattr(multipliers_proxy, "_selected_account_type", selected_account_type)

    async def consume_one_client_message(session, upstream, websocket):
        await websocket.send_text(json.dumps({"msg_type": "test"}))

    async def drain_provider_messages(session, upstream, websocket):
        return None

    monkeypatch.setattr(multipliers_proxy, "handle_client_messages", consume_one_client_message)
    monkeypatch.setattr(multipliers_proxy, "handle_provider_messages", drain_provider_messages)

    class FakeUpstream:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def recv(self):
            return json.dumps({"msg_type": "authorize", "authorize": {"loginid": account_id}})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    upstream = FakeUpstream()

    class FakeConnector:
        async def __aenter__(self):
            return upstream

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(multipliers_proxy.websockets, "connect", lambda *args, **kwargs: FakeConnector())

    class FakeWebSocket:
        headers = {"origin": "https://frontend.example", "sec-websocket-protocol": f"tradebridge, {ticket}"}
        query_params = {"account_id": account_id}

        def __init__(self):
            self.accepted = False
            self.messages = []
            self.closed_code = None

        async def accept(self, subprotocol=None):
            self.accepted = subprotocol == "tradebridge"

        async def send_json(self, message):
            self.messages.append(message)

        async def close(self, code=1000, reason=None):
            self.closed_code = code

        async def send_text(self, message):
            self.messages.append(json.loads(message))

        async def receive_text(self):
            raise WebSocketDisconnect(code=1000)

    websocket = FakeWebSocket()
    await multipliers_proxy.deriv_multipliers_websocket_proxy(websocket)

    assert websocket.accepted is True
    assert websocket.closed_code is None, websocket.messages
    assert upstream.sent == [{"authorize": "oauth-access-token", "req_id": 1}]
    assert websocket.messages[0]["type"] == "multipliers_connection_status"
    assert websocket.messages[0]["account_id"] == account_id


@pytest.mark.anyio
async def test_multipliers_selected_account_must_be_owned_and_supported(monkeypatch):
    from fastapi import HTTPException
    from app.deriv import multipliers_proxy

    class FakeResponse:
        status_code = 200
        is_success = True

        def json(self):
            return {"data": {"accounts": [
                {"account_id": "VRTC123", "account_type": "demo"},
                {"account_id": "CR123456", "account_type": "real"},
            ]}}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    async def linked_accounts(user_id, account_ids):
        return {"CR123456"}

    monkeypatch.setattr(multipliers_proxy.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(multipliers_proxy, "_linked_account_ids", linked_accounts)

    assert await multipliers_proxy._selected_account_type("user-1", "CR123456", "token") == "real"
    with pytest.raises(HTTPException) as exc:
        await multipliers_proxy._selected_account_type("user-1", "VRTC123", "token")
    assert exc.value.status_code == 403

