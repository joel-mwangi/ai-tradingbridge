from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.even_odd import collection_jobs


USER_ID = UUID("00000000-0000-0000-0000-000000000111")
JOB_ID = "00000000-0000-0000-0000-000000000222"


def test_collection_auth_requires_backend_token_and_user_context(monkeypatch):
    monkeypatch.setenv("TRADEBRIDGE_API_TOKEN", "server-token")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="server-token")

    assert collection_jobs._auth(credentials, str(USER_ID)) == USER_ID

    with pytest.raises(HTTPException) as missing:
        collection_jobs._auth(None, str(USER_ID))
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as invalid_user:
        collection_jobs._auth(credentials, "not-a-uuid")
    assert invalid_user.value.status_code == 401


@pytest.mark.asyncio
async def test_start_collection_rejects_unsupported_target_before_database_access():
    request = collection_jobs.StartCollectionRequest(symbol="R_50", target_ticks=300)

    with pytest.raises(HTTPException) as error:
        await collection_jobs.start_collection(request, USER_ID)

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_start_collection_rejects_second_active_job(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [{"id": JOB_ID, "status": "collecting"}]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return Response()

        async def post(self, *args, **kwargs):
            raise AssertionError("A duplicate active job must not be inserted.")

    monkeypatch.setattr(collection_jobs, "_configuration", lambda: ("https://db.example", "secret"))
    monkeypatch.setattr(collection_jobs.httpx, "AsyncClient", FakeClient)
    request = collection_jobs.StartCollectionRequest(symbol="R_50", target_ticks=1000)

    with pytest.raises(HTTPException) as error:
        await collection_jobs.start_collection(request, USER_ID)

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_start_collection_creates_requested_live_job(monkeypatch):
    job = {
        "id": JOB_ID, "owner_user_id": str(USER_ID), "market_symbol": "R_50",
        "target_ticks": 2500, "received_ticks": 0, "status": "queued",
    }

    class Response:
        def __init__(self, rows, status_code=201):
            self.rows = rows
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("failure", request=None, response=None)

        def json(self):
            return self.rows

    class FakeClient:
        def __init__(self, **kwargs):
            self.insert = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return Response([])

        async def post(self, url, **kwargs):
            self.insert = kwargs["json"]
            return Response([job])

    monkeypatch.setattr(collection_jobs, "_configuration", lambda: ("https://db.example", "secret"))
    monkeypatch.setattr(collection_jobs.httpx, "AsyncClient", FakeClient)
    request = collection_jobs.StartCollectionRequest(symbol="R_50", target_ticks=2500)

    result = await collection_jobs.start_collection(request, USER_ID)

    assert result["job"] == job
    assert result["live_only"] is True
    assert result["order_execution_enabled"] is False


@pytest.mark.asyncio
async def test_stop_collection_is_scoped_to_job_and_owner(monkeypatch):
    stopped = {"id": JOB_ID, "owner_user_id": str(USER_ID), "status": "stopped"}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [stopped]

    class FakeClient:
        def __init__(self, **kwargs):
            self.call = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def patch(self, url, *, params, headers, json):
            self.call = {"params": params, "json": json}
            return Response()

    fake = FakeClient()
    monkeypatch.setattr(collection_jobs, "_configuration", lambda: ("https://db.example", "secret"))
    monkeypatch.setattr(collection_jobs.httpx, "AsyncClient", lambda **kwargs: fake)

    result = await collection_jobs.stop_collection(UUID(JOB_ID), USER_ID)

    assert result["job"] == stopped
    assert fake.call["params"]["id"] == f"eq.{JOB_ID}"
    assert fake.call["params"]["owner_user_id"] == f"eq.{USER_ID}"
    assert fake.call["json"]["status"] == "stopped"


@pytest.mark.asyncio
async def test_live_tick_batch_metadata_links_back_to_manual_job(monkeypatch):
    from app.even_odd import storage

    calls = {}

    async def fake_start(**kwargs):
        calls["start"] = kwargs
        return "00000000-0000-0000-0000-000000000333"

    async def fake_finish(**kwargs):
        calls["finish"] = kwargs

    monkeypatch.setattr(storage, "start_collection_run", fake_start)
    monkeypatch.setattr(storage, "finish_collection_run", fake_finish)

    tick = {
        "epoch": 1795037706,
        "quote": "83.2081",
        "pip_size": 4,
        "last_digit": 1,
        "parity": "odd",
        "received_at": "2026-11-20T00:00:01+00:00",
    }
    await storage.persist_live_tick_batch(
        symbol="R_50",
        ticks=[tick],
        worker_id="worker-test",
        job_id=JOB_ID,
    )

    metadata = calls["start"]["metadata"]
    assert metadata["capture_mode"] == "live_stream"
    assert metadata["batch_kind"] == "manual_job_batch"
    assert metadata["job_id"] == JOB_ID
    assert calls["finish"]["capture_mode"] == "live_stream"
    assert calls["finish"]["status"] == "partial"


@pytest.mark.asyncio
async def test_browser_tick_batch_calls_atomic_rpc_with_owner_and_stable_batch_id(monkeypatch):
    batch_id = UUID("00000000-0000-0000-0000-000000000444")
    job = {
        "id": JOB_ID, "market_symbol": "R_50", "target_ticks": 500,
        "received_ticks": 1, "status": "collecting",
    }
    result_payload = {
        "job": job, "persisted_ticks": 1, "batch_id": str(batch_id),
        "duplicate": False, "provenance": "browser_stream",
    }

    class Response:
        status_code = 200

        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return self.rows

    class FakeClient:
        def __init__(self, **kwargs):
            self.rpc_url = None
            self.rpc_payload = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return Response([{"id": JOB_ID, "market_symbol": "R_50"}])

        async def post(self, url, **kwargs):
            self.rpc_url = url
            self.rpc_payload = kwargs["json"]
            return Response(result_payload)

    fake = FakeClient()
    monkeypatch.setattr(collection_jobs, "_configuration", lambda: ("https://db.example", "secret"))
    monkeypatch.setattr(collection_jobs.httpx, "AsyncClient", lambda **kwargs: fake)

    request = collection_jobs.LiveTickBatchRequest(
        batch_id=batch_id,
        ticks=[{
            "tick": {
                "symbol": "R_50", "quote": "83.2081", "epoch": 1795037706,
                "pip_size": 4, "id": "deriv-tick-1",
            },
            "subscription_id": "subscription-1",
        }],
    )
    result = await collection_jobs.persist_browser_tick_batch(
        UUID(JOB_ID), request, USER_ID
    )

    assert result["duplicate"] is False
    assert result["provenance"] == "browser_stream"
    assert fake.rpc_url.endswith("/rpc/even_odd_persist_browser_tick_batch")
    assert fake.rpc_payload["p_job_id"] == JOB_ID
    assert fake.rpc_payload["p_owner_user_id"] == str(USER_ID)
    assert fake.rpc_payload["p_batch_id"] == str(batch_id)
    assert fake.rpc_payload["p_ticks"][0]["tick_id"] == "deriv-tick-1"
    assert fake.rpc_payload["p_ticks"][0]["stream_id"] == "subscription-1"


@pytest.mark.asyncio
async def test_browser_tick_batch_maps_rpc_batch_id_conflict_to_409(monkeypatch):
    batch_id = UUID("00000000-0000-0000-0000-000000000445")

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return Response([{"id": JOB_ID, "market_symbol": "R_50"}])

        async def post(self, *args, **kwargs):
            return Response({"error": "batch_id was already used with a different payload"})

    monkeypatch.setattr(collection_jobs, "_configuration", lambda: ("https://db.example", "secret"))
    monkeypatch.setattr(collection_jobs.httpx, "AsyncClient", FakeClient)
    request = collection_jobs.LiveTickBatchRequest(
        batch_id=batch_id,
        ticks=[{
            "tick": {
                "symbol": "R_50", "quote": "83.2081", "epoch": 1795037706,
                "pip_size": 4, "id": "deriv-tick-1",
            },
            "subscription_id": "subscription-1",
        }],
    )

    with pytest.raises(HTTPException) as error:
        await collection_jobs.persist_browser_tick_batch(UUID(JOB_ID), request, USER_ID)

    assert error.value.status_code == 409
    assert "different payload" in error.value.detail
