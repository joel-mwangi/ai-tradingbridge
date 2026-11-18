import httpx
import pytest
from datetime import datetime, timezone

from app.even_odd.storage import (
    EvenOddStorageError,
    _configuration,
    _headers,
    _mark_collection_failed,
    persist_collection,
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
