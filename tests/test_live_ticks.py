import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app

client = TestClient(app)


def test_live_ticks_rejects_invalid_symbol_before_accepting():
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/live/ticks?symbol=bad%2Fsymbol"):
            pass


def test_live_ticks_fails_closed_without_deriv_app_id(monkeypatch):
    monkeypatch.delenv("DERIV_APP_ID", raising=False)
    with client.websocket_connect("/live/ticks?symbol=1HZ100V") as websocket:
        message = websocket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "deriv_app_id_missing"
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()
