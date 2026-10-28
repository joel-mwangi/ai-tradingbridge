from fastapi.testclient import TestClient

from app import main, mt5_bridge
from app.deriv.security import current_user

client = TestClient(main.app)
USER_ID = "d3c9a5f4-4f72-4ec3-8f5e-22b4d7e6d7a1"


def setup_function():
    main.app.dependency_overrides[current_user] = lambda: USER_ID


def teardown_function():
    main.app.dependency_overrides.pop(current_user, None)


def test_create_pairing_code_stores_only_hash(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    captured = {}

    async def fake_request(method, path, **kwargs):
        captured.update(method=method, path=path, payload=kwargs.get("payload"))
        return None

    monkeypatch.setattr(mt5_bridge, "_request", fake_request)
    response = client.post("/mt5/pairing-codes", json={"expires_in_seconds": 300})

    assert response.status_code == 200
    raw_code = response.json()["code"]
    assert len(raw_code) >= 32
    assert captured["path"] == "mt5_pairing_codes"
    assert captured["payload"]["user_id"] == USER_ID
    assert captured["payload"]["code_hash"] != raw_code
    assert captured["payload"]["code_hash"] == mt5_bridge._digest(raw_code)


def test_pair_device_returns_one_time_credential_and_hashes_it(monkeypatch):
    captured = {}

    async def fake_rpc(name, payload):
        captured.update(name=name, payload=payload)
        return [{"device_id": "bdc8c2e4-1d21-44e4-91e2-832ca2a22c7a", "owner_user_id": USER_ID}]

    monkeypatch.setattr(mt5_bridge, "_rpc", fake_rpc)
    response = client.post("/mt5/pair", json={"code": "x" * 40, "device_name": "MT5 terminal"})

    assert response.status_code == 200
    credential = response.json()["device_credential"]
    assert len(credential) >= 32
    assert response.json()["credential_shown_once"] is True
    assert captured["name"] == "mt5_pair_device"
    assert captured["payload"]["p_code_hash"] == mt5_bridge._digest("x" * 40)
    assert captured["payload"]["p_credential_hash"] == mt5_bridge._digest(credential)
    assert credential not in str(captured["payload"])


def test_create_market_command_checks_owner_and_is_select_market_only(monkeypatch):
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "mt5_devices" and method == "GET":
            return [{"id": "bdc8c2e4-1d21-44e4-91e2-832ca2a22c7a", "status": "paired"}]
        if path == "mt5_commands" and method == "GET":
            return []
        if path == "mt5_commands" and method == "POST":
            return [{
                "id": "c4a3a0c0-5a50-49ce-a9a0-08d3c1e930b2",
                "command_type": "select_market",
                "provider_symbol": "EURUSD",
                "status": "pending",
            }]
        raise AssertionError(f"Unexpected storage call: {method} {path}")

    monkeypatch.setattr(mt5_bridge, "_request", fake_request)
    response = client.post(
        "/mt5/devices/bdc8c2e4-1d21-44e4-91e2-832ca2a22c7a/commands",
        json={"provider_symbol": "EURUSD", "idempotency_key": "test-key-0001"},
    )

    assert response.status_code == 201
    assert response.json()["command"]["command_type"] == "select_market"
    assert response.json()["command"]["provider_symbol"] == "EURUSD"
    assert not any("buy" in path.lower() or "order" in path.lower() for _, path, _ in calls)


def test_invalid_market_symbol_rejected_before_storage(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("Storage must not be called for invalid symbols")

    monkeypatch.setattr(mt5_bridge, "_request", fail_if_called)
    response = client.post(
        "/mt5/devices/bdc8c2e4-1d21-44e4-91e2-832ca2a22c7a/commands",
        json={"provider_symbol": "../EURUSD", "idempotency_key": "test-key-0001"},
    )
    assert response.status_code == 422


def test_ea_next_command_requires_device_credential():
    response = client.get("/mt5/ea/commands/next")
    assert response.status_code == 401


def test_revoked_device_cannot_poll(monkeypatch):
    async def fake_request(method, path, **kwargs):
        if path == "mt5_devices":
            return [{"id": "bdc8c2e4-1d21-44e4-91e2-832ca2a22c7a", "status": "revoked"}]
        raise AssertionError("Revoked device must not reach command leasing")

    monkeypatch.setattr(mt5_bridge, "_request", fake_request)
    response = client.get(
        "/mt5/ea/commands/next",
        headers={"Authorization": "Bearer " + "x" * 64},
    )
    assert response.status_code == 403
