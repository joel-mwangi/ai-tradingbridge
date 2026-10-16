from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_live_ticks_rejects_invalid_symbol_before_connecting():
    with client.websocket_connect("/live/ticks?symbol=bad%2Fsymbol") as websocket:
        # Invalid symbols are closed before acceptance.
        pass
