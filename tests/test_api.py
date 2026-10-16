import httpx
import pytest
from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)
TOKEN = "test-token"


@pytest.fixture(autouse=True)
def api_token(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_API_TOKEN", TOKEN)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_does_not_disclose_secret_configuration():
    response = client.get("/health")
    assert response.status_code == 200
    assert "database_configured" not in response.json()


def test_analysis_requires_authentication():
    assert client.post("/analysis/digits", json={"digits": [1, 2]}).status_code == 401
    assert client.get("/analysis/digits/latest").status_code == 401


def test_analysis_fails_closed_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("TRADEBRIDGE_API_TOKEN", raising=False)
    response = client.post("/analysis/digits", json={"digits": [1, 2]})
    assert response.status_code == 503


def test_digit_analysis_success():
    response = client.post(
        "/analysis/digits", json={"digits": [1, 1, 2]}, headers=auth()
    )
    assert response.status_code == 200
    assert response.json()["sample_size"] == 3


def test_latest_requires_supabase_configuration():
    response = client.get("/analysis/digits/latest", headers=auth())
    assert response.status_code == 503


def test_latest_rejects_invalid_symbol_before_database_call(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "server-secret")
    response = client.get(
        "/analysis/digits/latest?symbol=bad%2Fsymbol", headers=auth()
    )
    assert response.status_code == 422


def test_latest_success(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "server-secret")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"symbol": "1HZ100V", "last_digit": 3},
                {"symbol": "1HZ100V", "last_digit": 7},
            ]

    def fake_get(url, **kwargs):
        assert url == "https://example.supabase.co/rest/v1/market_ticks"
        assert kwargs["headers"]["Authorization"] == "Bearer server-secret"
        assert kwargs["timeout"] <= 10
        assert kwargs["params"]["limit"] == 1000
        return Response()

    monkeypatch.setattr(main.httpx, "get", fake_get)
    response = client.get("/analysis/digits/latest", headers=auth())
    assert response.status_code == 200
    assert response.json()["returned_ticks"] == 2


def test_latest_empty_results(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "server-secret")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    monkeypatch.setattr(main.httpx, "get", lambda *args, **kwargs: Response())
    response = client.get("/analysis/digits/latest", headers=auth())
    assert response.status_code == 404


@pytest.mark.parametrize("rows", [
    {"unexpected": "object"},
    [{"symbol": "1HZ100V", "last_digit": True}],
    [{"symbol": "1HZ100V", "last_digit": 12}],
    [{"symbol": "OTHER", "last_digit": 1}],
    ["not-a-row"],
])
def test_latest_rejects_malformed_database_rows(monkeypatch, rows):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "server-secret")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return rows

    monkeypatch.setattr(main.httpx, "get", lambda *args, **kwargs: Response())
    response = client.get("/analysis/digits/latest", headers=auth())
    assert response.status_code == 502


def test_latest_hides_upstream_errors(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "server-secret")

    def fail(*args, **kwargs):
        raise httpx.TimeoutException("secret upstream detail")

    monkeypatch.setattr(main.httpx, "get", fail)
    response = client.get("/analysis/digits/latest", headers=auth())
    assert response.status_code == 502
    assert "secret upstream detail" not in response.text
    assert "server-secret" not in response.text
