import pytest

from app.even_odd.storage import (
    EvenOddStorageError,
    _configuration,
    _headers,
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
