import json
from datetime import datetime, timezone
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
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
    assert result["sample"]["current_parity_streak"] == 2
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
    assert tick["stream_id"] == "x"


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
    assert client.get("/even-odd/history?symbol=R_50").status_code == 401
    assert client.post("/even-odd/sessions", json={}).status_code == 401
    assert client.post("/even-odd/sequences", json={}).status_code == 401
    assert client.post("/even-odd/settlements", json={}).status_code == 401
    assert client.get("/even-odd/sessions/00000000-0000-0000-0000-000000000444").status_code == 401
    assert client.get("/even-odd/sequences/00000000-0000-0000-0000-000000000555").status_code == 401
    assert client.get("/even-odd/models?symbol=R_50").status_code == 401
    assert client.get("/even-odd/research-runs?symbol=R_50").status_code == 401
    assert client.get(
        "/even-odd/forecast?symbol=R_50&model_id=00000000-0000-0000-0000-000000000333"
    ).status_code == 401


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


def test_collect_endpoint_uses_dedicated_collector_and_persists(monkeypatch):
    sample = [
        {
            "received_at": "2026-11-18T00:00:00+00:00",
            "epoch": 1760000000 + i,
            "symbol": "R_50",
            "quote": f"123.45{i % 10}",
            "pip_size": 3,
            "last_digit": i % 10,
            "parity": "even" if i % 2 == 0 else "odd",
            "stream_id": "stream-123",
        }
        for i in range(10)
    ]

    collection_started_at = []

    async def fake_collect(symbol, tick_count, timeout_seconds):
        assert collection_started_at[0] <= datetime.now(timezone.utc)
        assert symbol == "R_50"
        assert tick_count == 10
        return sample

    async def fake_start_collection_run(*, symbol, requested_ticks, started_at):
        assert symbol == "R_50"
        assert requested_ticks == 10
        assert isinstance(started_at, datetime)
        assert started_at.tzinfo == timezone.utc
        collection_started_at.append(started_at)
        return "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10"

    async def fake_finish_collection_run(**kwargs):
        assert kwargs["collection_id"] == "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10"
        assert kwargs["symbol"] == "R_50"
        assert kwargs["requested_ticks"] == 10
        assert kwargs["ticks"] == sample
        assert kwargs["status"] == "completed"

    monkeypatch.setattr("app.even_odd.router.collect_ticks", fake_collect)
    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.start_collection_run", fake_start_collection_run)
    monkeypatch.setattr("app.even_odd.router.finish_collection_run", fake_finish_collection_run)
    response = client.post(
        "/even-odd/collect",
        headers=auth(),
        json={"symbol": "R_50", "tick_count": 10},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["returned_ticks"] == 10
    assert payload["persisted"] is True
    assert payload["collection_id"] == "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10"
    assert payload["analysis"]["sample"]["sample_size"] == 10
    assert payload["predictive_edge_established"] is False


def test_collect_endpoint_fails_closed_without_dedicated_storage(monkeypatch):
    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: False)
    response = client.post(
        "/even-odd/collect",
        headers=auth(),
        json={"symbol": "R_50", "tick_count": 10},
    )
    assert response.status_code == 503



def test_collect_endpoint_persists_partial_ticks_and_returns_auditable_failure(monkeypatch):
    from app.even_odd.collector import TickCollectionError

    partial = [
        {
            "received_at": "2026-11-19T00:00:01+00:00",
            "epoch": 1760000000 + i,
            "symbol": "R_50",
            "quote": f"123.45{i}",
            "pip_size": 3,
            "last_digit": i,
            "parity": "even" if i % 2 == 0 else "odd",
            "stream_id": "stream-123",
        }
        for i in range(3)
    ]
    saved = {}

    async def fake_start_collection_run(**kwargs):
        return "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10"

    async def fake_collect(**kwargs):
        raise TickCollectionError("Deriv feed disconnected.", ticks=partial)

    async def fake_finish_collection_run(**kwargs):
        saved.update(kwargs)

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.start_collection_run", fake_start_collection_run)
    monkeypatch.setattr("app.even_odd.router.collect_ticks", fake_collect)
    monkeypatch.setattr("app.even_odd.router.finish_collection_run", fake_finish_collection_run)

    response = client.post(
        "/even-odd/collect",
        headers=auth(),
        json={"symbol": "R_50", "tick_count": 10},
    )
    assert response.status_code == 502
    assert saved["ticks"] == partial
    assert saved["status"] == "partial"
    assert saved["error_message"] == "Deriv feed disconnected."
    assert response.json()["detail"]["status"] == "partial"
    assert response.json()["detail"]["received_ticks"] == 3


def test_collect_endpoint_marks_zero_tick_failure(monkeypatch):
    from app.even_odd.collector import TickCollectionError

    failed = {}

    async def fake_start_collection_run(**kwargs):
        return "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10"

    async def fake_collect(**kwargs):
        raise TickCollectionError("Unable to connect.")

    async def fake_fail_collection_run(**kwargs):
        failed.update(kwargs)

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.start_collection_run", fake_start_collection_run)
    monkeypatch.setattr("app.even_odd.router.collect_ticks", fake_collect)
    monkeypatch.setattr("app.even_odd.router.fail_collection_run", fake_fail_collection_run)

    response = client.post(
        "/even-odd/collect",
        headers=auth(),
        json={"symbol": "R_50", "tick_count": 10},
    )
    assert response.status_code == 502
    assert failed["collection_id"] == "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10"
    assert failed["received_ticks"] == 0
    assert response.json()["detail"]["status"] == "failed"



@pytest.mark.asyncio
async def test_collector_retains_valid_ticks_when_deriv_feed_errors(monkeypatch):
    import json
    from app.even_odd.collector import TickCollectionError, collect_ticks

    messages = [
        json.dumps({
            "msg_type": "tick",
            "tick": {
                "symbol": "R_50",
                "quote": f"123.45{i}",
                "epoch": 1760000000 + i,
                "pip_size": 3,
                "id": "stream-123",
            },
        })
        for i in range(3)
    ]
    messages.append(json.dumps({"msg_type": "error", "error": {"message": "upstream detail"}}))

    class FakeSocket:
        async def send(self, payload):
            return None

        async def recv(self):
            return messages.pop(0)

    class FakeConnection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "app.even_odd.collector.websockets.connect",
        lambda *args, **kwargs: FakeConnection(),
    )

    with pytest.raises(TickCollectionError) as error:
        await collect_ticks("R_50", 10, 5)
    assert len(error.value.ticks) == 3
    assert all(tick["symbol"] == "R_50" for tick in error.value.ticks)


@pytest.mark.asyncio
async def test_collector_retains_valid_ticks_when_timeout_occurs(monkeypatch):
    import asyncio
    import json
    from app.even_odd.collector import TickCollectionError, collect_ticks

    messages = [
        json.dumps({
            "msg_type": "tick",
            "tick": {
                "symbol": "R_50",
                "quote": f"123.45{i}",
                "epoch": 1760000000 + i,
                "pip_size": 3,
                "id": "stream-123",
            },
        })
        for i in range(4)
    ]

    class FakeSocket:
        async def send(self, payload):
            return None

        async def recv(self):
            if messages:
                return messages.pop(0)
            raise asyncio.TimeoutError()

    class FakeConnection:
        async def __aenter__(self):
            return FakeSocket()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "app.even_odd.collector.websockets.connect",
        lambda *args, **kwargs: FakeConnection(),
    )

    with pytest.raises(TickCollectionError) as error:
        await collect_ticks("R_50", 10, 5)
    assert len(error.value.ticks) == 4



def test_history_endpoint_returns_bounded_chronological_snapshot(monkeypatch):
    snapshot = {
        "snapshot_max_id": 42,
        "rows": [
            {
                "id": 10,
                "collection_id": "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10",
                "sequence_no": 1,
                "market_symbol": "R_50",
                "tick_epoch": 1760000000,
                "quote": "123.450",
                "pip_size": 3,
                "last_digit": 0,
                "parity": "even",
                "deriv_stream_id": "stream-123",
                "received_at": "2026-11-19T00:00:01+00:00",
            }
        ],
    }
    captured = {}

    async def fake_load_historical_ticks(**kwargs):
        captured.update(kwargs)
        return snapshot

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_historical_ticks)

    response = client.get(
        "/even-odd/history?symbol=R_50&limit=100&from_epoch=1760000000",
        headers=auth(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["returned_ticks"] == 1
    assert payload["snapshot_max_id"] == 42
    assert payload["limit_reached"] is False
    assert payload["rows"][0]["last_digit"] == 0
    assert captured == {
        "symbol": "R_50",
        "limit": 100,
        "from_epoch": 1760000000,
        "to_epoch": None,
        "collection_id": None,
    }


def test_history_endpoint_rejects_inverted_epoch_range():
    response = client.get(
        "/even-odd/history?symbol=R_50&from_epoch=1760000002&to_epoch=1760000001",
        headers=auth(),
    )
    assert response.status_code == 422



def test_quality_endpoint_audits_historical_snapshot(monkeypatch):
    rows = [
        {
            "id": 1,
            "collection_id": "b7b6a6d0-4f49-4d5b-90f5-6e7a7e0e7f10",
            "sequence_no": 1,
            "market_symbol": "R_50",
            "tick_epoch": 1760000000,
            "quote": "123.452",
            "pip_size": 3,
            "last_digit": 2,
            "parity": "even",
            "deriv_stream_id": "stream-123",
            "received_at": "2026-11-19T00:00:01+00:00",
        }
    ]

    async def fake_load_historical_ticks(**kwargs):
        return {"rows": rows, "snapshot_max_id": 1}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_historical_ticks)

    response = client.get("/even-odd/quality?symbol=R_50", headers=auth())
    assert response.status_code == 200
    payload = response.json()
    assert payload["quality"]["quality_passed"] is True
    assert payload["quality"]["valid_rows"] == 1
    assert payload["snapshot_max_id"] == 1



def test_create_session_endpoint_validates_and_persists_fixed_stake(monkeypatch):
    captured = {}

    async def fake_create_session(**kwargs):
        captured.update(kwargs)
        return {"id": "session-1", "market_symbol": "R_50", "fixed_stake": "1.00"}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.create_even_odd_session", fake_create_session)

    response = client.post(
        "/even-odd/sessions",
        headers=auth(),
        json={
            "symbol": "R_50",
            "account_mode": "research",
            "currency": "USD",
            "fixed_stake": "1.00",
            "max_session_loss": "10.00",
        },
    )
    assert response.status_code == 200
    assert response.json()["order_execution_enabled"] is False
    assert captured["fixed_stake"] == "1.00"
    assert captured["max_trades_per_session"] == 10


def test_start_sequence_endpoint_uses_validated_model_gate(monkeypatch):
    captured = {}

    async def fake_start_sequence(**kwargs):
        captured.update(kwargs)
        return {
            "sequence_id": "sequence-1",
            "planned_trades": 3,
            "status": "active",
            "execution_authorized": False,
        }

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.start_even_odd_sequence", fake_start_sequence)

    response = client.post(
        "/even-odd/sequences",
        headers=auth(),
        json={
            "session_id": "00000000-0000-0000-0000-000000000444",
            "model_id": "00000000-0000-0000-0000-000000000333",
            "model_version": "v1",
            "planned_trades": 3,
            "expected_value_snapshot": {"trade_1": 0.25},
        },
    )
    assert response.status_code == 200
    assert response.json()["order_execution_enabled"] is False
    assert captured["planned_trades"] == 3
    assert captured["model_version"] == "v1"


def test_settlement_endpoint_records_result_without_order_execution(monkeypatch):
    captured = {}

    async def fake_settlement(**kwargs):
        captured.update(kwargs)
        return {
            "trade_id": "trade-1",
            "sequence_status": "completed",
            "session_status": "active",
            "execution_authorized": False,
        }

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.record_even_odd_settlement", fake_settlement)

    response = client.post(
        "/even-odd/settlements",
        headers=auth(),
        json={
            "sequence_id": "00000000-0000-0000-0000-000000000555",
            "trade_number": 1,
            "side": "even",
            "stake": "1.00",
            "profit_if_win": "0.80",
            "loss_if_lose": "1.00",
            "even_probability": "0.80",
            "lower_even_probability": "0.75",
            "upper_even_probability": "0.85",
            "conservative_expected_value": "0.35",
            "revalidation": {
                "model_validated": True,
                "uncertainty_validated": True,
                "signal_valid": True,
                "data_fresh": True,
                "risk_budget_available": True,
            },
            "outcome": "win",
            "realized_profit": "0.80",
        },
    )
    assert response.status_code == 200
    assert response.json()["order_execution_enabled"] is False
    assert captured["side"] == "even"
    assert captured["revalidation"]["uncertainty_validated"] is True


def test_session_endpoint_rejects_stake_escalation_policy_values():
    response = client.post(
        "/even-odd/sessions",
        headers=auth(),
        json={
            "symbol": "R_50",
            "account_mode": "research",
            "currency": "USD",
            "fixed_stake": "1.00",
            "max_trades_per_session": 11,
        },
    )
    assert response.status_code == 422



def test_session_endpoint_requires_explicit_monetary_loss_cap():
    response = client.post(
        "/even-odd/sessions",
        headers=auth(),
        json={
            "symbol": "R_50",
            "account_mode": "research",
            "currency": "USD",
            "fixed_stake": "1.00",
        },
    )
    assert response.status_code == 422



def test_get_session_state_returns_persisted_sequences(monkeypatch):
    async def fake_get_session(session_id):
        assert session_id == "00000000-0000-0000-0000-000000000444"
        return {
            "session": {"id": session_id, "status": "active", "completed_trades": 2},
            "sequences": [{"id": "sequence-1", "status": "active"}],
        }

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_get_session)

    response = client.get(
        "/even-odd/sessions/00000000-0000-0000-0000-000000000444",
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["session"]["completed_trades"] == 2
    assert response.json()["sequences"][0]["status"] == "active"


def test_get_sequence_state_returns_settled_trade_history(monkeypatch):
    async def fake_get_sequence(sequence_id):
        assert sequence_id == "00000000-0000-0000-0000-000000000555"
        return {
            "sequence": {"id": sequence_id, "planned_trades": 3, "status": "active"},
            "trades": [{"trade_number": 1, "outcome": "win"}],
        }

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_sequence_state", fake_get_sequence)

    response = client.get(
        "/even-odd/sequences/00000000-0000-0000-0000-000000000555",
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["trades"][0]["outcome"] == "win"



def test_list_models_endpoint_returns_metadata_only(monkeypatch):
    async def fake_list_models(**kwargs):
        assert kwargs == {"symbol": "R_50", "limit": 20}
        return [{
            "id": "model-1",
            "model_version": "v1",
            "status": "candidate",
            "execution_authorized": False,
            "metrics": {"test_log_loss": 0.69},
        }]

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.list_even_odd_models", fake_list_models)

    response = client.get(
        "/even-odd/models?symbol=R_50&limit=20",
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["models"][0]["status"] == "candidate"
    assert "model_artifact" not in response.json()["models"][0]


def test_list_research_runs_endpoint_returns_persisted_experiments(monkeypatch):
    async def fake_list_runs(**kwargs):
        assert kwargs == {"symbol": "R_50", "limit": 50}
        return [{"id": "run-1", "status": "completed", "dataset_rows": 4000}]

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.list_even_odd_research_runs", fake_list_runs)

    response = client.get("/even-odd/research-runs?symbol=R_50", headers=auth())
    assert response.status_code == 200
    assert response.json()["research_runs"][0]["status"] == "completed"



def forecast_fixture(*, status="candidate", age_seconds=1, duplicate=False):
    from app.even_odd.ml.features import build_next_tick_features

    now_epoch = int(datetime.now(timezone.utc).timestamp())
    digits = [0, 1, 2, 3]
    features = build_next_tick_features(digits, windows=(4,))
    feature_names = sorted(features)
    rows = []
    for index, digit in enumerate(digits, start=1):
        rows.append({
            "id": index,
            "collection_id": "collection-1",
            "sequence_no": index,
            "market_symbol": "R_50",
            "tick_epoch": now_epoch - age_seconds - (4 - index),
            "quote": f"100.{digit}",
            "pip_size": 1,
            "last_digit": digit,
            "parity": "even" if digit % 2 == 0 else "odd",
            "deriv_stream_id": "stream-1",
            "received_at": "2026-11-19T00:00:00+00:00",
        })
    if duplicate:
        rows[-1]["sequence_no"] = rows[-2]["sequence_no"]
    model = {
        "id": "00000000-0000-0000-0000-000000000333",
        "market_symbol": "R_50",
        "model_version": "v1",
        "status": status,
        "model_artifact": {
            "feature_names": feature_names,
            "means": [0.0] * len(feature_names),
            "scales": [1.0] * len(feature_names),
            "weights": [0.0] * len(feature_names),
            "intercept": 0.4,
        },
        "metrics": {
            "walk_forward": {
                "calibration_intervals": [{
                    "validated": True,
                    "target_probability": 0.6,
                    "probability_bandwidth": 0.1,
                    "lower_probability": 0.55,
                    "upper_probability": 0.65,
                    "method": "walk_forward_moving_block_bootstrap_local_calibration",
                    "confidence_level": 0.9,
                }]
            }
        },
    }
    return model, rows


def test_forecast_endpoint_returns_research_only_probability_and_interval(monkeypatch):
    model, rows = forecast_fixture()

    async def fake_get_model(model_id):
        return model

    async def fake_load_history(**kwargs):
        assert kwargs == {"symbol": "R_50", "limit": 4}
        return {"rows": rows, "snapshot_max_id": 4}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.get(
        "/even-odd/forecast?symbol=R_50&model_id=00000000-0000-0000-0000-000000000333",
        headers=auth(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert 0 <= payload["probability_even"] <= 1
    assert payload["model_validated"] is False
    assert payload["uncertainty_validated"] is True
    assert payload["entry_gate_prerequisites_passed"] is False
    assert payload["entry_candidate"] is False
    assert payload["execution_authorized"] is False
    assert payload["order_execution_enabled"] is False


def test_forecast_endpoint_rejects_stale_or_corrupt_market_data(monkeypatch):
    model, stale_rows = forecast_fixture(age_seconds=60)

    async def fake_get_model(model_id):
        return model

    async def fake_stale_history(**kwargs):
        return {"rows": stale_rows, "snapshot_max_id": 4}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_stale_history)

    url = "/even-odd/forecast?symbol=R_50&model_id=00000000-0000-0000-0000-000000000333"
    stale = client.get(url, headers=auth())
    assert stale.status_code == 409

    model, duplicate_rows = forecast_fixture(duplicate=True)

    async def fake_duplicate_history(**kwargs):
        return {"rows": duplicate_rows, "snapshot_max_id": 4}

    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_duplicate_history)
    duplicate = client.get(url, headers=auth())
    assert duplicate.status_code == 409


def test_forecast_endpoint_rejects_retired_models(monkeypatch):
    model, _ = forecast_fixture(status="retired")

    async def fake_get_model(model_id):
        return model

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)

    response = client.get(
        "/even-odd/forecast?symbol=R_50&model_id=00000000-0000-0000-0000-000000000333",
        headers=auth(),
    )
    assert response.status_code == 409
