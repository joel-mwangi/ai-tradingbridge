from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import main
from app.even_odd.collector import normalize_tick, normalize_live_tick
from app.even_odd.digit_engine import analyze_digits


TOKEN = "even-odd-test-token"
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def configure_api_token(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_API_TOKEN", TOKEN)


def auth():
    return {"Authorization": f"Bearer {TOKEN}", "X-TradeBridge-User-ID": "00000000-0000-0000-0000-000000000777"}


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


def test_normalize_live_tick_keeps_tick_and_subscription_ids_separate():
    tick = normalize_live_tick(
        {
            "symbol": "R_50",
            "quote": "123.450",
            "epoch": 1760000000,
            "pip_size": 3,
            "id": "tick-id-1",
        },
        "R_50",
        subscription_id="subscription-id-1",
    )
    assert tick["tick_id"] == "tick-id-1"
    assert tick["stream_id"] == "subscription-id-1"
    assert tick["last_digit"] == 0


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
    assert client.post("/even-odd/entry-check", json={}).status_code == 401


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


def test_historical_collection_endpoint_is_retired():
    response = client.post(
        "/even-odd/collect",
        headers=auth(),
        json={"symbol": "R_50", "tick_count": 10},
    )
    assert response.status_code == 410
    assert "Historical tick collection is disabled" in response.json()["detail"]


def test_live_status_reports_no_worker_until_heartbeat_exists(monkeypatch):
    async def no_heartbeat(*, symbol):
        assert symbol == "R_50"
        return None

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_live_collector_heartbeat", no_heartbeat)
    response = client.get("/even-odd/live-status?symbol=R_50", headers=auth())
    assert response.status_code == 200
    assert response.json()["collector"] is None
    assert response.json()["live_only"] is True
    assert response.json()["historical_imports_enabled"] is False
    assert response.json()["order_execution_enabled"] is False


def test_history_endpoint_returns_bounded_live_capture_snapshot(monkeypatch):
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



def test_quality_endpoint_audits_live_capture_snapshot(monkeypatch):
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
    assert captured["owner_user_id"] == "00000000-0000-0000-0000-000000000777"
    assert captured["fixed_stake"] == "1.00"
    assert captured["max_trades_per_session"] == 10


def test_session_creation_requires_verified_user_context():
    response = client.post(
        "/even-odd/sessions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "symbol": "R_50",
            "account_mode": "research",
            "currency": "USD",
            "fixed_stake": "1.00",
            "max_session_loss": "10.00",
        },
    )
    assert response.status_code == 401


def test_start_sequence_endpoint_uses_server_recomputed_plan(monkeypatch):
    captured = {}

    async def fake_plan(request, owner_user_id):
        assert str(request.session_id) == "00000000-0000-0000-0000-000000000444"
        assert len(request.observations) == 3
        return {
            "status": "plan",
            "planned_trades": 3,
            "plan_basis": "test_proxy",
            "plan_reason": "Three candidates passed.",
            "expected_value_by_trade": [0.2, 0.18, 0.15],
            "conservative_ev_by_trade": [0.1, 0.09, 0.08],
            "forecasts": [{"trade_number": 1}],
            "payout_quote": {"source": "deriv_public_websocket"},
            "execution_authorized": False,
        }

    async def fake_start_sequence(**kwargs):
        captured.update(kwargs)
        return {
            "sequence_id": "sequence-1",
            "planned_trades": kwargs["planned_trades"],
            "status": "active",
            "execution_authorized": False,
        }

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.plan_adaptive_sequence", fake_plan)
    monkeypatch.setattr("app.even_odd.router.start_even_odd_sequence", fake_start_sequence)

    response = client.post(
        "/even-odd/sequences",
        headers=auth(),
        json={
            "session_id": "00000000-0000-0000-0000-000000000444",
            "model_id": "00000000-0000-0000-0000-000000000333",
            "model_version": "v1",
            "observations": [
                {"tick_row_id": 22, "tick_epoch": 1_799_999_998},
                {"tick_row_id": 23, "tick_epoch": 1_799_999_999},
                {"tick_row_id": 24, "tick_epoch": 1_800_000_000},
            ],
            # These legacy fields must not control the server's plan.
            "planned_trades": 7,
            "expected_value_snapshot": {"trade_1": 999},
        },
    )
    assert response.status_code == 200
    assert response.json()["order_execution_enabled"] is False
    assert response.json()["sequence_started"] is True
    assert response.json()["sequence_plan"]["planned_trades"] == 3
    assert captured["planned_trades"] == 3
    assert captured["expected_value_snapshot"]["plan_basis"] == "test_proxy"
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
    async def fake_get_session(session_id, owner_user_id):
        assert session_id == "00000000-0000-0000-0000-000000000444"
        assert str(owner_user_id) == "00000000-0000-0000-0000-000000000777"
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
    async def fake_get_sequence(sequence_id, owner_user_id):
        assert sequence_id == "00000000-0000-0000-0000-000000000555"
        assert str(owner_user_id) == "00000000-0000-0000-0000-000000000777"
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
        return {"rows": rows, "snapshot_max_id": 24}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_entry_session_state)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
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



def entry_history_fixture():
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    rows = []
    for index in range(1, 25):
        digit = index % 10
        rows.append({
            "id": index,
            "collection_id": "entry-collection",
            "sequence_no": index,
            "market_symbol": "R_50",
            "tick_epoch": now_epoch - (24 - index),
            "quote": f"100.{digit}",
            "pip_size": 1,
            "last_digit": digit,
            "parity": "even" if digit % 2 == 0 else "odd",
            "deriv_stream_id": "entry-stream",
            "received_at": "2026-11-19T00:00:00+00:00",
        })
    return rows


def entry_check_payload(rows):
    return {
        "session_id": "00000000-0000-0000-0000-000000000444",
        "observations": [
            {
                "tick_row_id": row["id"],
                "tick_epoch": row["tick_epoch"],
                "model_id": "00000000-0000-0000-0000-000000000333",
                "model_version": "v1",
            }
            for row in rows[-3:]
        ],
    }


def entry_session_state():
    return {
        "session": {
            "id": "00000000-0000-0000-0000-000000000444",
            "status": "active",
            "market_symbol": "R_50",
            "currency": "USD",
            "fixed_stake": "1.00",
            "max_session_loss": "10.00",
            "realized_profit": "0.00",
            "completed_trades": 0,
            "max_trades_per_session": 10,
            "consecutive_losses": 0,
            "max_consecutive_losses": 2,
        },
        "sequences": [],
    }


async def fake_entry_session_state(session_id, owner_user_id):
    assert session_id == "00000000-0000-0000-0000-000000000444"
    return entry_session_state()


async def fake_entry_payout_quote(*, symbol, currency, stake):
    assert symbol == "R_50"
    assert currency == "USD"
    assert str(stake) == "1.00"
    return {
        "source": "deriv_public_websocket",
        "symbol": symbol,
        "currency": currency,
        "requested_stake": "1.00",
        "duration": 1,
        "duration_unit": "t",
        "quoted_at": datetime.now(timezone.utc).isoformat(),
        "even": {
            "contract_type": "DIGITEVEN",
            "proposal_id": "even-proposal",
            "ask_price": "1.00",
            "payout": "1.80",
            "profit_if_win": "0.80",
            "loss_if_lose": "1.00",
        },
        "odd": {
            "contract_type": "DIGITODD",
            "proposal_id": "odd-proposal",
            "ask_price": "1.00",
            "payout": "1.90",
            "profit_if_win": "0.90",
            "loss_if_lose": "1.00",
        },
        "execution_authorized": False,
    }


def entry_model(*, status="validated", interval_validated=True, version="v1"):
    from app.even_odd.ml.features import build_next_tick_features

    feature_names = sorted(
        build_next_tick_features([index % 10 for index in range(20)], windows=(20,))
    )
    return {
        "id": "00000000-0000-0000-0000-000000000333",
        "market_symbol": "R_50",
        "model_version": version,
        "status": status,
        "model_artifact": {
            "feature_names": feature_names,
            "means": [0.0] * len(feature_names),
            "scales": [1.0] * len(feature_names),
            "weights": [0.0] * len(feature_names),
            "intercept": 1.3862943611198906,
        },
        "metrics": {
            "walk_forward": {
                "calibration_intervals": [{
                    "validated": interval_validated,
                    "target_probability": 0.8,
                    "probability_bandwidth": 0.1,
                    "lower_probability": 0.75,
                    "upper_probability": 0.85,
                    "method": "walk_forward_moving_block_bootstrap_local_calibration",
                    "confidence_level": 0.9,
                }]
            },
            "payout_replay": {
                "evidence_sample_sufficient": True,
                "entry_count": 500,
                "positive_fold_rate": 0.8,
                "average_conservative_expected_value": 0.12,
                "net_profit": 50.0,
                "roi_on_staked": 0.1,
                "sequence_persistence_by_horizon": {
                    "1": {"validated": True, "lower_confidence_bound_90": 0.7},
                    "2": {"validated": True, "lower_confidence_bound_90": 0.6},
                    "3": {"validated": True, "lower_confidence_bound_90": 0.5},
                },
            },
        },
    }


def test_hybrid_entry_endpoint_returns_non_executable_candidate_when_all_gates_pass(monkeypatch):
    rows = entry_history_fixture()

    async def fake_get_model(model_id):
        assert model_id == "00000000-0000-0000-0000-000000000333"
        return entry_model()

    async def fake_load_history(**kwargs):
        assert kwargs == {"symbol": "R_50", "limit": 5000}
        return {"rows": rows, "snapshot_max_id": 24}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_entry_session_state)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=entry_check_payload(rows),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "candidate", payload
    assert payload["side"] == "even"
    assert payload["model_status"] == "validated"
    assert payload["payout_terms_source"] == "deriv_public_websocket"
    assert payload["payout_quote"]["even"]["profit_if_win"] == "0.80"
    assert payload["model_probability_even"] == pytest.approx(0.8)
    assert payload["latest_observation_sample_size"] >= 20
    assert payload["execution_authorized"] is False
    assert payload["order_execution_enabled"] is False


def test_hybrid_entry_endpoint_waits_for_model_and_uncertainty_validation(monkeypatch):
    rows = entry_history_fixture()

    async def fake_candidate_model(model_id):
        return entry_model(status="candidate")

    async def fake_load_history(**kwargs):
        return {"rows": rows, "snapshot_max_id": 23}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_candidate_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_entry_session_state)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=entry_check_payload(rows),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "wait"

    async def fake_unvalidated_interval(model_id):
        return entry_model(interval_validated=False)

    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_unvalidated_interval)
    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=entry_check_payload(rows),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "wait"


def test_hybrid_entry_endpoint_rejects_stale_model_version(monkeypatch):
    async def fake_old_model(model_id):
        return entry_model(version="v2")

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_old_model)

    rows = entry_history_fixture()
    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=entry_check_payload(rows),
    )
    assert response.status_code == 409



def test_collect_request_rejects_timeout_above_hobby_safe_limit():
    response = client.post(
        "/even-odd/collect",
        headers=auth(),
        json={"symbol": "R_50", "tick_count": 10, "timeout_seconds": 81},
    )
    assert response.status_code == 422


def test_hybrid_entry_endpoint_rejects_tick_ids_not_in_persisted_history(monkeypatch):
    rows = entry_history_fixture()

    async def fake_get_model(model_id):
        return entry_model()

    async def fake_load_history(**kwargs):
        return {"rows": rows, "snapshot_max_id": 24}

    payload = entry_check_payload(rows)
    payload["observations"][0]["tick_row_id"] = 9999

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_entry_session_state)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=payload,
    )
    assert response.status_code == 409



def test_settlement_revalidation_flags_reject_string_boolean_coercion():
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
                "model_validated": "true",
                "uncertainty_validated": True,
                "signal_valid": True,
                "data_fresh": True,
                "risk_budget_available": True,
            },
            "outcome": "win",
            "realized_profit": "0.80",
        },
    )
    assert response.status_code == 422


def test_entry_check_waits_when_session_loss_budget_is_exhausted(monkeypatch):
    async def fake_get_model(model_id):
        return entry_model()

    async def fake_session(session_id, owner_user_id):
        state = entry_session_state()
        state["session"]["realized_profit"] = "-10.00"
        return state

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_session)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)

    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=entry_check_payload(entry_history_fixture()),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "wait"
    assert "loss budget" in response.json()["reason"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("completed_trades", 10), ("consecutive_losses", 2)],
)
def test_entry_check_blocks_exhausted_session_trade_limits(monkeypatch, field, value):
    async def fake_get_model(model_id):
        return entry_model()

    async def fake_session(session_id, owner_user_id):
        state = entry_session_state()
        state["session"][field] = value
        return state

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_session)

    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=entry_check_payload(entry_history_fixture()),
    )
    assert response.status_code == 409


def test_entry_check_does_not_allow_client_to_relax_freshness_policy(monkeypatch):
    rows = entry_history_fixture()
    for row in rows:
        row["tick_epoch"] -= 100

    async def fake_get_model(model_id):
        return entry_model()

    async def fake_session(session_id, owner_user_id):
        return entry_session_state()

    async def fake_load_history(**kwargs):
        return {"rows": rows, "snapshot_max_id": 24}

    payload = entry_check_payload(rows)
    payload["policy"] = {
        "minimum_sample_size": 1,
        "maximum_data_age_seconds": 1000,
        "required_confirmations": 2,
        "maximum_confirmation_gap_seconds": 1000,
        "minimum_edge_over_break_even": 0,
        "minimum_conservative_expected_value": 0,
    }

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_session)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=payload,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "wait"
    assert "stale" in response.json()["reason"].lower()



def sequence_plan_payload(rows):
    return {
        "session_id": "00000000-0000-0000-0000-000000000444",
        "model_id": "00000000-0000-0000-0000-000000000333",
        "model_version": "v1",
        "observations": [
            {"tick_row_id": row["id"], "tick_epoch": row["tick_epoch"]}
            for row in rows[-4:]
        ],
    }


def test_sequence_plan_uses_server_forecasts_and_selects_bounded_recent_persistence(monkeypatch):
    rows = entry_history_fixture()

    async def fake_get_model(model_id):
        return entry_model()

    async def fake_load_history(**kwargs):
        return {"rows": rows, "snapshot_max_id": 24}

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_entry_session_state)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.post(
        "/even-odd/sequence-plan",
        headers=auth(),
        json=sequence_plan_payload(rows),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "plan"
    assert payload["planned_trades"] == 4
    assert payload["max_allowed_trades"] <= 7
    assert payload["plan_basis"] == "walk_forward_oos_opportunity_persistence_and_payout_replay"
    assert payload["persistence_lower_bound_by_trade"] == [1.0, 0.7, 0.6, 0.5]
    assert payload["sequence_adjusted_conservative_ev_by_trade"]
    assert payload["execution_authorized"] is False
    assert payload["order_execution_enabled"] is False


def test_sequence_plan_waits_for_model_validation_and_risk_budget(monkeypatch):
    rows = entry_history_fixture()

    async def fake_candidate_model(model_id):
        return entry_model(status="candidate")

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_candidate_model)

    response = client.post(
        "/even-odd/sequence-plan",
        headers=auth(),
        json=sequence_plan_payload(rows),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "wait"
    assert response.json()["planned_trades"] == 0

    async def fake_validated_model(model_id):
        return entry_model()

    async def fake_low_budget_session(session_id, owner_user_id):
        state = entry_session_state()
        state["session"]["max_session_loss"] = "0.50"
        return state

    async def fake_load_history(**kwargs):
        return {"rows": rows, "snapshot_max_id": 24}

    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_validated_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_low_budget_session)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.post(
        "/even-odd/sequence-plan",
        headers=auth(),
        json=sequence_plan_payload(rows),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "wait"
    assert response.json()["planned_trades"] == 0



def test_hybrid_entry_endpoint_rejects_cherry_picked_nonconsecutive_ticks(monkeypatch):
    rows = entry_history_fixture()

    async def fake_get_model(model_id):
        return entry_model()

    async def fake_load_history(**kwargs):
        return {"rows": rows, "snapshot_max_id": 24}

    payload = entry_check_payload(rows)
    skipped_tick = rows[-4]
    payload["observations"][0] = {
        "tick_row_id": skipped_tick["id"],
        "tick_epoch": skipped_tick["tick_epoch"],
        "model_id": "00000000-0000-0000-0000-000000000333",
        "model_version": "v1",
    }

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_entry_session_state)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_entry_payout_quote)
    monkeypatch.setattr("app.even_odd.router.load_historical_ticks", fake_load_history)

    response = client.post(
        "/even-odd/entry-check",
        headers=auth(),
        json=payload,
    )
    assert response.status_code == 409



def test_sequence_plan_rejects_a_session_with_an_active_sequence(monkeypatch):
    rows = entry_history_fixture()

    async def fake_get_model(model_id):
        return entry_model()

    async def fake_active_session(session_id, owner_user_id):
        state = entry_session_state()
        state["sequences"] = [{"status": "active"}]
        return state

    monkeypatch.setattr("app.even_odd.router.storage_is_configured", lambda: True)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_model", fake_get_model)
    monkeypatch.setattr("app.even_odd.router.get_even_odd_session_state", fake_active_session)

    response = client.post(
        "/even-odd/sequence-plan",
        headers=auth(),
        json=sequence_plan_payload(rows),
    )
    assert response.status_code == 409



def test_payouts_endpoint_returns_read_only_even_and_odd_quotes(monkeypatch):
    async def fake_quote(**kwargs):
        assert kwargs == {"symbol": "R_50", "currency": "USD", "stake": Decimal("1")}
        return {
            "even": {"contract_type": "DIGITEVEN", "proposal_id": "even"},
            "odd": {"contract_type": "DIGITODD", "proposal_id": "odd"},
            "execution_authorized": False,
        }

    monkeypatch.setattr("app.even_odd.router.get_even_odd_payout_quote", fake_quote)
    response = client.get(
        "/even-odd/payouts?symbol=R_50&currency=USD&stake=1",
        headers=auth(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["even"]["contract_type"] == "DIGITEVEN"
    assert payload["odd"]["contract_type"] == "DIGITODD"
    assert payload["execution_authorized"] is False



def test_start_sequence_schema_uses_persisted_observation_references():
    from app.even_odd.schemas import StartSequenceRequest

    request = StartSequenceRequest.model_validate({
        "session_id": "00000000-0000-0000-0000-000000000444",
        "model_id": "00000000-0000-0000-0000-000000000333",
        "model_version": "v1",
        "observations": [
            {"tick_row_id": 22, "tick_epoch": 1_799_999_998},
            {"tick_row_id": 23, "tick_epoch": 1_799_999_999},
            {"tick_row_id": 24, "tick_epoch": 1_800_000_000},
        ],
    })
    assert request.observations[0].tick_row_id == 22
