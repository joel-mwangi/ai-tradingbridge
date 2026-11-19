import httpx
import pytest
from datetime import datetime, timezone

from app.even_odd.storage import (
    EvenOddStorageError,
    _configuration,
    _headers,
    _mark_collection_failed,
    persist_collection,
    save_candidate_model,
    start_research_run,
    complete_research_run,
    fail_research_run,
    create_even_odd_session,
    start_even_odd_sequence,
    record_even_odd_settlement,
    get_even_odd_session_state,
    get_even_odd_sequence_state,
    storage_is_configured,
)


def test_storage_requires_dedicated_environment_variables(monkeypatch):
    monkeypatch.delenv("EVEN_ODD_SUPABASE_URL", raising=False)
    monkeypatch.delenv("EVEN_ODD_SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("EVEN_ODD_SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://primary.example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "primary-secret")

    assert storage_is_configured() is False
    with pytest.raises(EvenOddStorageError):
        _configuration()


def test_storage_accepts_dedicated_secret_key(monkeypatch):
    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co/")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "dedicated-secret")
    monkeypatch.delenv("EVEN_ODD_SUPABASE_SERVICE_ROLE_KEY", raising=False)

    assert _configuration() == (
        "https://dedicated.example.supabase.co",
        "dedicated-secret",
    )


def test_storage_supports_legacy_dedicated_service_role_key(monkeypatch):
    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.delenv("EVEN_ODD_SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SERVICE_ROLE_KEY", "dedicated-legacy-secret")

    assert _configuration() == (
        "https://dedicated.example.supabase.co",
        "dedicated-legacy-secret",
    )


def test_opaque_secret_key_is_sent_only_as_apikey():
    headers = _headers("sb_secret_example")

    assert headers["apikey"] == "sb_secret_example"
    assert "Authorization" not in headers


def test_legacy_service_role_jwt_uses_bearer_and_apikey():
    headers = _headers("legacy-service-role-jwt")

    assert headers["apikey"] == "legacy-service-role-jwt"
    assert headers["Authorization"] == "Bearer legacy-service-role-jwt"


@pytest.mark.asyncio
async def test_failed_collection_update_sets_terminal_status_without_sensitive_details():
    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self):
            self.call = None

        async def patch(self, url, *, params, headers, json):
            self.call = {
                "url": url,
                "params": params,
                "headers": headers,
                "json": json,
            }
            return Response()

    client = Client()
    await _mark_collection_failed(
        client,
        url="https://dedicated.example.supabase.co",
        key="sb_secret_example",
        collection_id="00000000-0000-0000-0000-000000000001",
    )

    assert client.call["url"].endswith("/even_odd_collection_runs")
    assert client.call["params"] == {
        "id": "eq.00000000-0000-0000-0000-000000000001"
    }
    assert client.call["json"]["status"] == "failed"
    assert client.call["json"]["completed_at"]
    assert "Inspect backend logs" in client.call["json"]["error_message"]
    assert client.call["headers"]["apikey"] == "sb_secret_example"
    assert "sb_secret_example" not in str(client.call["json"])


@pytest.mark.asyncio
async def test_failure_status_update_is_best_effort_and_does_not_raise():
    class Response:
        def raise_for_status(self):
            raise httpx.HTTPError("database unavailable")

    class Client:
        async def patch(self, *args, **kwargs):
            return Response()

    await _mark_collection_failed(
        Client(),
        url="https://dedicated.example.supabase.co",
        key="sb_secret_example",
        collection_id="00000000-0000-0000-0000-000000000001",
    )



@pytest.mark.asyncio
async def test_persist_collection_uses_collection_start_and_stream_id(monkeypatch):
    collection_id = "00000000-0000-0000-0000-000000000123"
    started_at = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
    sample = [{
        "epoch": 1795037706,
        "quote": "83.2081",
        "pip_size": 4,
        "last_digit": 1,
        "parity": "odd",
        "stream_id": "deriv-stream-abc",
        "received_at": "2026-11-19T00:00:01+00:00",
    }]

    class Response:
        def __init__(self, rows=None):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return self.rows

    class FakeAsyncClient:
        instances = []

        def __init__(self, **kwargs):
            self.posts = []
            self.patches = []
            FakeAsyncClient.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            self.posts.append({"url": url, "json": json})
            if url.endswith("/even_odd_collection_runs"):
                return Response([{"id": collection_id}])
            return Response()

        async def patch(self, url, *, params, headers, json):
            self.patches.append({"url": url, "json": json})
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await persist_collection(
        symbol="R_50",
        requested_ticks=1,
        ticks=sample,
        started_at=started_at,
    )

    assert result == collection_id
    start_client, finish_client = FakeAsyncClient.instances
    run_payload = start_client.posts[0]["json"]
    tick_payload = finish_client.posts[0]["json"][0]
    assert run_payload["started_at"] == started_at.isoformat()
    assert tick_payload["deriv_stream_id"] == "deriv-stream-abc"
    assert "deriv_tick_id" not in tick_payload
    assert finish_client.patches[0]["json"]["status"] == "completed"



@pytest.mark.asyncio
async def test_load_historical_ticks_uses_stable_snapshot_and_paginates(monkeypatch):
    from app.even_odd.storage import load_historical_ticks

    first_page = [
        {
            "id": i + 1,
            "tick_epoch": 1760000000 + i,
            "last_digit": i % 10,
            "market_symbol": "R_50",
        }
        for i in range(1000)
    ]
    second_page = [
        {
            "id": 1001,
            "tick_epoch": 1760001000,
            "last_digit": 1,
            "market_symbol": "R_50",
        },
        {
            "id": 1002,
            "tick_epoch": 1760001001,
            "last_digit": 2,
            "market_symbol": "R_50",
        },
    ]

    class Response:
        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return self.rows

    class FakeAsyncClient:
        instance = None

        def __init__(self, **kwargs):
            self.calls = []
            FakeAsyncClient.instance = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, params, headers):
            self.calls.append({"params": params, "headers": headers})
            if params.get("order") == "id.desc":
                return Response([{"id": 1002}])
            if headers["Range"] == "0-999":
                return Response(first_page)
            if headers["Range"] == "1000-1001":
                return Response(second_page)
            raise AssertionError(f"Unexpected range: {headers['Range']}")

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await load_historical_ticks(
        symbol="R_50",
        limit=1002,
        from_epoch=1760000000,
        to_epoch=1760001001,
    )

    assert len(result["rows"]) == 1002
    assert result["snapshot_max_id"] == 1002
    client = FakeAsyncClient.instance
    assert len(client.calls) == 3
    data_call = client.calls[1]
    assert data_call["params"]["market_symbol"] == "eq.R_50"
    assert data_call["params"]["id"] == "lte.1002"
    assert data_call["params"]["order"] == "tick_epoch.desc,id.desc"
    assert data_call["params"]["and"] == (
        "(tick_epoch.gte.1760000000,tick_epoch.lte.1760001001)"
    )
    assert client.calls[1]["headers"]["Range"] == "0-999"
    assert client.calls[2]["headers"]["Range"] == "1000-1001"


@pytest.mark.asyncio
async def test_load_historical_ticks_returns_empty_when_database_has_no_ticks(monkeypatch):
    from app.even_odd.storage import load_historical_ticks

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await load_historical_ticks(symbol="R_50")
    assert result == {"rows": [], "snapshot_max_id": None}



@pytest.mark.asyncio
async def test_start_research_run_persists_snapshot_and_quality(monkeypatch):
    run_id = "00000000-0000-0000-0000-000000000222"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": run_id}]

    class FakeAsyncClient:
        payload = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            FakeAsyncClient.payload = json
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await start_research_run(
        symbol="R_50",
        snapshot_max_id=100,
        dataset_rows=90,
        quality_report={"quality_passed": True},
    )
    assert result == run_id
    assert FakeAsyncClient.payload["snapshot_max_id"] == 100
    assert FakeAsyncClient.payload["dataset_rows"] == 90
    assert FakeAsyncClient.payload["status"] == "running"


@pytest.mark.asyncio
async def test_candidate_model_is_persisted_with_execution_disabled(monkeypatch):
    model_id = "00000000-0000-0000-0000-000000000333"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": model_id}]

    class FakeAsyncClient:
        payload = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            FakeAsyncClient.payload = json
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await save_candidate_model(
        symbol="R_50",
        model_version="20261119T000000000000Z",
        model_artifact={"model_type": "standardized_logistic_regression"},
        metrics={"test": {"log_loss": 0.69}},
        snapshot_max_id=100,
        partition_sizes={
            "train": 50,
            "validation": 20,
            "calibration": 20,
            "test": 20,
        },
    )
    payload = FakeAsyncClient.payload
    assert result == model_id
    assert payload["status"] == "candidate"
    assert payload["execution_authorized"] is False
    assert payload["model_artifact"]["model_type"] == "standardized_logistic_regression"


@pytest.mark.asyncio
async def test_complete_research_run_links_candidate_model_and_stores_metrics(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": "00000000-0000-0000-0000-000000000222"}]

    class FakeAsyncClient:
        call = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def patch(self, url, *, params, headers, json):
            FakeAsyncClient.call = {"params": params, "headers": headers, "json": json}
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    await complete_research_run(
        research_run_id="00000000-0000-0000-0000-000000000222",
        model_id="00000000-0000-0000-0000-000000000333",
        partition_sizes={"train": 50, "validation": 20, "calibration": 20, "test": 20},
        evaluation={"test_log_loss": 0.69},
    )
    assert FakeAsyncClient.call["params"]["id"] == "eq.00000000-0000-0000-0000-000000000222"
    assert FakeAsyncClient.call["json"]["status"] == "completed"
    assert FakeAsyncClient.call["json"]["model_id"] == "00000000-0000-0000-0000-000000000333"
    assert FakeAsyncClient.call["json"]["evaluation"]["test_log_loss"] == 0.69


@pytest.mark.asyncio
async def test_fail_research_run_is_best_effort_without_configuration(monkeypatch):
    monkeypatch.delenv("EVEN_ODD_SUPABASE_URL", raising=False)
    monkeypatch.delenv("EVEN_ODD_SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.delenv("EVEN_ODD_SUPABASE_SERVICE_ROLE_KEY", raising=False)
    await fail_research_run(
        research_run_id="00000000-0000-0000-0000-000000000222"
    )



@pytest.mark.asyncio
async def test_create_even_odd_session_uses_dedicated_storage(monkeypatch):
    session_id = "00000000-0000-0000-0000-000000000444"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": session_id, "market_symbol": "R_50"}]

    class FakeAsyncClient:
        payload = None
        url = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            FakeAsyncClient.url = url
            FakeAsyncClient.payload = json
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await create_even_odd_session(
        owner_user_id="00000000-0000-0000-0000-000000000777",
        symbol="R_50",
        account_mode="research",
        currency="USD",
        fixed_stake="1.00",
        max_session_loss="10.00",
    )
    assert result["id"] == session_id
    assert FakeAsyncClient.url.endswith("/rest/v1/even_odd_sessions")
    assert FakeAsyncClient.payload["owner_user_id"] == "00000000-0000-0000-0000-000000000777"
    assert FakeAsyncClient.payload["fixed_stake"] == "1.00"
    assert FakeAsyncClient.payload["max_trades_per_session"] == 10


@pytest.mark.asyncio
async def test_start_sequence_calls_atomic_database_function(monkeypatch):
    sequence_id = "00000000-0000-0000-0000-000000000555"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sequence_id": sequence_id, "execution_authorized": False}

    class FakeAsyncClient:
        payload = None
        url = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            FakeAsyncClient.url = url
            FakeAsyncClient.payload = json
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await start_even_odd_sequence(
        session_id="00000000-0000-0000-0000-000000000444",
        owner_user_id="00000000-0000-0000-0000-000000000777",
        model_id="00000000-0000-0000-0000-000000000333",
        model_version="v1",
        planned_trades=4,
        expected_value_snapshot={"trade_1": 0.2},
    )
    assert result["sequence_id"] == sequence_id
    assert FakeAsyncClient.url.endswith("/rest/v1/rpc/even_odd_start_sequence")
    assert FakeAsyncClient.payload["p_owner_user_id"] == "00000000-0000-0000-0000-000000000777"
    assert FakeAsyncClient.payload["p_planned_trades"] == 4


@pytest.mark.asyncio
async def test_settlement_calls_atomic_database_function(monkeypatch):
    trade_id = "00000000-0000-0000-0000-000000000666"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"trade_id": trade_id, "sequence_status": "completed"}

    class FakeAsyncClient:
        payload = None
        url = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            FakeAsyncClient.url = url
            FakeAsyncClient.payload = json
            return Response()

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await record_even_odd_settlement(
        sequence_id="00000000-0000-0000-0000-000000000555",
        owner_user_id="00000000-0000-0000-0000-000000000777",
        trade_number=1,
        side="even",
        stake="1",
        profit_if_win="0.8",
        loss_if_lose="1",
        even_probability="0.8",
        lower_even_probability="0.75",
        upper_even_probability="0.85",
        conservative_expected_value="0.35",
        revalidation={
            "model_validated": True,
            "uncertainty_validated": True,
            "signal_valid": True,
            "data_fresh": True,
            "risk_budget_available": True,
        },
        outcome="win",
        realized_profit="0.8",
    )
    assert result["trade_id"] == trade_id
    assert FakeAsyncClient.url.endswith("/rest/v1/rpc/even_odd_record_trade_settlement")
    assert FakeAsyncClient.payload["p_owner_user_id"] == "00000000-0000-0000-0000-000000000777"
    assert FakeAsyncClient.payload["p_conservative_expected_value"] == "0.35"

@pytest.mark.asyncio
async def test_session_read_is_filtered_by_owner_user_id(monkeypatch):
    session_id = "00000000-0000-0000-0000-000000000444"
    owner_id = "00000000-0000-0000-0000-000000000777"

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeAsyncClient:
        calls = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, params, headers):
            FakeAsyncClient.calls.append((url, params))
            if url.endswith("/even_odd_sessions"):
                return Response([{"id": session_id, "owner_user_id": owner_id}])
            return Response([{"id": "sequence-1", "session_id": session_id}])

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await get_even_odd_session_state(session_id, owner_id)

    assert result is not None
    assert FakeAsyncClient.calls[0][1]["owner_user_id"] == f"eq.{owner_id}"


@pytest.mark.asyncio
async def test_sequence_read_denies_when_parent_session_is_not_owned(monkeypatch):
    sequence_id = "00000000-0000-0000-0000-000000000555"
    session_id = "00000000-0000-0000-0000-000000000444"
    owner_id = "00000000-0000-0000-0000-000000000777"

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeAsyncClient:
        calls = []

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, params, headers):
            FakeAsyncClient.calls.append(url)
            if url.endswith("/even_odd_sequences"):
                return Response([{"id": sequence_id, "session_id": session_id}])
            if url.endswith("/even_odd_sessions"):
                return Response([])
            raise AssertionError("Trade records must not be queried for an unowned sequence.")

    monkeypatch.setenv("EVEN_ODD_SUPABASE_URL", "https://dedicated.example.supabase.co")
    monkeypatch.setenv("EVEN_ODD_SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setattr("app.even_odd.storage.httpx.AsyncClient", FakeAsyncClient)

    result = await get_even_odd_sequence_state(sequence_id, owner_id)

    assert result is None
    assert len(FakeAsyncClient.calls) == 2

