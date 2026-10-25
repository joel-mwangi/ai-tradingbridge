"""Server-owned Deriv credential custody and account-link storage.

OAuth credentials are encrypted at rest in a server-only Supabase table. The
encryption key must remain separate from the database.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException


@dataclass
class Credential:
    access_token: str
    expires_at: float
    updated_at: float


def _credential_cipher() -> Fernet:
    key = os.getenv("TRADEBRIDGE_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="Credential encryption is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise HTTPException(status_code=503, detail="Credential encryption key is invalid.")


def _credential_store_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not service_key:
        raise HTTPException(status_code=503, detail="Credential storage is not configured.")
    return url, service_key


async def _store_credential(user_id: str, access_token: str, expires_in: int) -> Credential:
    url, service_key = _credential_store_config()
    encrypted = _credential_cipher().encrypt(access_token.encode("utf-8")).decode("ascii")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)
    payload = {
        "user_id": user_id,
        "encrypted_access_token": encrypted,
        "expires_at": expires_at.isoformat(),
        "updated_at": now.isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{url}/rest/v1/deriv_backend_credentials",
                params={"on_conflict": "user_id"},
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                json=payload,
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not store the encrypted Deriv credential.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not store the encrypted Deriv credential.")
    return Credential(access_token=access_token, expires_at=expires_at.timestamp(), updated_at=now.timestamp())


async def _load_credential(user_id: str) -> Credential | None:
    url, service_key = _credential_store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                f"{url}/rest/v1/deriv_backend_credentials",
                params={
                    "select": "encrypted_access_token,expires_at,updated_at",
                    "user_id": f"eq.{user_id}",
                    "limit": "1",
                },
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not load the encrypted Deriv credential.")
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(status_code=503, detail="Credential storage returned invalid data.")
        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict) or not isinstance(row.get("encrypted_access_token"), str):
            raise HTTPException(status_code=503, detail="Credential storage returned invalid data.")
        expires_at = datetime.fromisoformat(str(row.get("expires_at")).replace("Z", "+00:00")).timestamp()
        updated_at = datetime.fromisoformat(str(row.get("updated_at")).replace("Z", "+00:00")).timestamp()
        try:
            access_token = _credential_cipher().decrypt(row["encrypted_access_token"].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeEncodeError):
            raise HTTPException(status_code=503, detail="Stored Deriv credential cannot be decrypted.")
        return Credential(access_token=access_token, expires_at=expires_at, updated_at=updated_at)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, OverflowError):
        raise HTTPException(status_code=503, detail="Could not load the encrypted Deriv credential.")


async def _delete_credential(user_id: str) -> None:
    url, service_key = _credential_store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.delete(
                f"{url}/rest/v1/deriv_backend_credentials",
                params={"user_id": f"eq.{user_id}"},
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not revoke the stored Deriv credential.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not revoke the stored Deriv credential.")


async def _store_account_links(user_id: str, account_ids: list[str]) -> None:
    """Upsert verified Deriv account IDs into deriv_account_links for ownership checks."""
    if not account_ids:
        return
    url, service_key = _credential_store_config()
    rows = [{"user_id": user_id, "deriv_account_id": aid} for aid in account_ids]
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{url}/rest/v1/deriv_account_links",
                params={"on_conflict": "user_id,deriv_account_id"},
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=ignore-duplicates,return=minimal",
                },
                json=rows,
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not store Deriv account links.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not store Deriv account links.")


async def _delete_account_links(user_id: str) -> None:
    """Remove all deriv_account_links rows for a user on disconnect or revoke."""
    url, service_key = _credential_store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.delete(
                f"{url}/rest/v1/deriv_account_links",
                params={"user_id": f"eq.{user_id}"},
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not remove Deriv account links.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not remove Deriv account links.")


async def _owned_credential(user_id: str) -> Credential:
    credential = await _load_credential(user_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="Deriv is not connected to this backend session.")
    if credential.expires_at <= time.time():
        await _delete_credential(user_id)
        raise HTTPException(status_code=401, detail="Deriv authorization expired; reconnect Deriv.")
    return credential


async def _linked_account_ids(user_id: str, account_ids: list[str]) -> set[str]:
    """Return only account IDs linked to this app user; fail closed on DB errors."""
    if not account_ids:
        return set()
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=503, detail="Secure account linking is not configured.")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                f"{supabase_url}/rest/v1/deriv_account_links",
                params={
                    "select": "deriv_account_id",
                    "user_id": f"eq.{user_id}",
                    "deriv_account_id": f"in.({','.join(account_ids)})",
                },
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Secure account linking could not be verified.")
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(status_code=503, detail="Secure account linking returned invalid data.")
        return {
            row["deriv_account_id"]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("deriv_account_id"), str)
        }
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Secure account linking could not be verified.")
