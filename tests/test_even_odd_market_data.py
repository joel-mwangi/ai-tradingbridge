import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.even_odd import collector
from app.even_odd.collector import normalize_tick
from app.even_odd.digit_engine import analyze_digits


TOKEN = "even-odd-test-token"
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def configure_api_token(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_API_TOKEN", TOKEN)


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_digit_engine_reports_parity_and_digit_frequencies():
    result = analyze_digits([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    sample = result["sample"]
    assert sample["sample_size"] == 10
    assert sample["even"] == {"count": 5, "percentage": 50.0}
    assert sample["odd"] == {"count": 5, "percentage": 50.0}
    assert sample["digit_counts"] == {str(i): 1 for i in range(10)}
    assert sample["latest_digit"] == 9
    assert sample["latest_parity"] == "odd"
    assert result["rolling_windows"] == {}


def test_digit_engine_includes_rolling_windows_and_streaks():
    digits = [2, 4, 6, 1, 3] * 10
    result = analyze_digits(digits)
    assert "50" in result["rolling_windows"]
    assert result["sample"]["current_parity_streak"] == 1
    assert result["sample"]["transitions"]["even_to_even"] > 0


@pytest.mark.parametrize("digits", [[], [0, 10], [1, -1], [True, 2], [1.0, 2]])
def test_digit_engine_rejects_invalid_digit_samples(digits):
    with pytest.raises(ValueError):
        analyze_digits(digits)


def test_normalize_tick_uses_decimal_precision_without_float_rounding():
    tick = normalize_tick(
        {"symbol": "R_50", "quote": "123.450", "epoch": 1760000000, "pip_size": 3, "id": "x"},
        "R_50",
    )
    assert tick["quote"] == "123.450"
    assert tick["last_digit"] == 0
    assert tick["parity"] == "even"


@pytest.mark.parametrize(
    "payload",
    [
        {"quote": 12.34, "epoch": 1760000000, "pip_size": 2},
        {"symbol": "OTHER", "quote": "12.34", "epoch": 1760000000, "pip_size": 2},
        {"symbol": "R_50", "quote": "NaN", "epoch": 1760000000, "pip_size": 2},
        {"symbol": "R_50", "quote": "12.34", "epoch": 1760000000, "pip_size": 11},
    ],
)
def test_normalize_tick_rejects_invalid_payloads(payload):
    with pytest.raises(ValueError):
        normalize_tick(payload, "R_50")


def test_even_odd_endpoints_require_authentication():
    assert client.get("/even-odd/status").status_code == 401
    assert client.post("/even-odd/analyze", json={"digits": [1, 2]}).status_code == 401
    assert client.post("/even-odd/collect", json={"symbol": "R_50"}).status_code == 401


def test_even_odd_status_is_research_only():
    response = client.get("/even-odd/status", headers=auth())
    assert response.status_code == 200
    assert response.json()["execution_enabled"] is False
    assert response.json()["predictive_model_available"] is False


def test_even_odd_analyze_endpoint():
    response = client.post(
        "/even-odd/analyze",
        headers=auth(),
        json={"digits": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]},
    )
    assert response.status_code == 200
    assert response.json()["analysis"]["sample"]["even"]["percentage"] == 50.0


@pytest.mark.asyncio
async def test_collect_endpoint_uses_dedicated_collector(monkeypatch):
    sample = [
        {
            "received_at": "2026-11-18T00:00:00+00:00",
            "epoch": 1760000000 + i,
            "symbol": "R_50",
            "quote": f"123.45{i % 10}",
            "pip_size": 3,
            "last_digit": i % 10,
            "parity": "even" if i % 2 == 0 else "odd",
            "tick_id": str(i),
        }
        for i in range(10)
    ]

    async def fake_collect(symbol, tick_count, timeout_seconds):
        assert symbol == "R_50"
        assert tick_count == 10
        return sample

    monkeypatch.setattr("app.even_odd.router.collect_ticks", fake_collect)
    response = client.post(
        "/even-odd/collect",
        headers=auth(),
        json={"symbol": "R_50", "tick_count": 10},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["returned_ticks"] == 10
    assert payload["analysis"]["sample"]["sample_size"] == 10
    assert payload["predictive_edge_established"] is False
