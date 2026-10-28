from fastapi.testclient import TestClient

from app import main, mt5_bridge
from app.deriv.security import current_user

client = TestClient(main.app)
USER_ID = "d3c9a5f4-4f72-4ec3-8f5e-22b4d7e6d7a1"
DEVICE_ID = "bdc8c2e4-1d21-44e4-91e2-832ca2a22c7a"
COMMAND_ID = "c4a3a0c0-5a50-49ce-a9a0-08d3c1e930b2"
LEASE_TOKEN = "7c10e2ef-5a40-4d77-b6b0-9f2f21a9ed41"


def setup_function():
    main.app.dependency_overrides[current_user] = lambda: USER_ID


def teardown_function():
    main.app.dependency_overrides.pop(current_user, None)


def test_ea_next_command_returns_lease_token(monkeypatch):
    async def fake_request(method, path, **kwargs):
        if path == "mt5_devices":
            return [{
                "id": DEVICE_ID,
                "user_id": USER_ID,
                "device_name": "MT5 terminal",
                "status": "paired",
            }]
        raise AssertionError(f"Unexpected storage call: {method} {path}")

    async def fake_rpc(name, payload):
        assert name == "mt5_lease_next_command"
        assert payload["p_device_id"] == DEVICE_ID
        return [{
            "id": COMMAND_ID,
            "command_type": "select_market",
            "provider_symbol": "EURUSD",
            "expires_at": "2026-10-28T14:00:00+00:00",
            "lease_token": LEASE_TOKEN,
        }]

    monkeypatch.setattr(mt5_bridge, "_request", fake_request)
    monkeypatch.setattr(mt5_bridge, "_rpc", fake_rpc)
    response = client.get(
        "/mt5/ea/commands/next",
        headers={"Authorization": "Bearer " + "x" * 64},
    )

    assert response.status_code == 200
    assert response.json()["command"]["lease_token"] == LEASE_TOKEN


def test_acknowledgement_passes_lease_token_to_database(monkeypatch):
    captured = {}

    async def fake_request(method, path, **kwargs):
        if path == "mt5_devices":
            return [{
                "id": DEVICE_ID,
                "user_id": USER_ID,
                "device_name": "MT5 terminal",
                "status": "paired",
            }]
        return None

    async def fake_rpc(name, payload):
        captured.update(name=name, payload=payload)
        return [{"id": COMMAND_ID, "status": "acknowledged"}]

    monkeypatch.setattr(mt5_bridge, "_request", fake_request)
    monkeypatch.setattr(mt5_bridge, "_rpc", fake_rpc)
    response = client.post(
        f"/mt5/ea/commands/{COMMAND_ID}/ack",
        headers={"Authorization": "Bearer " + "x" * 64},
        json={"lease_token": LEASE_TOKEN, "outcome": "acknowledged"},
    )

    assert response.status_code == 200
    assert captured["name"] == "mt5_ack_command"
    assert captured["payload"]["p_lease_token"] == LEASE_TOKEN


def test_acknowledgement_requires_lease_token(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("Database must not be called without a lease token")

    monkeypatch.setattr(mt5_bridge, "_rpc", fail_if_called)
    response = client.post(
        f"/mt5/ea/commands/{COMMAND_ID}/ack",
        headers={"Authorization": "Bearer " + "x" * 64},
        json={"outcome": "acknowledged"},
    )

    assert response.status_code == 422
