""""Persistence adapter for the isolated Even/Odd Supabase project.

Only backend-side Even/Odd environment variables are accepted here. This
module deliberately never falls back to the application's primary Supabase
configuration.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx


class EvenOddStorageError(RuntimeError):
    """Raised when the dedicated Even/Odd database is unavailable."""


def _configuration() -> tuple[str, str]:
    url = os.getenv("EVEN_ODD_SUPABASE_URL", "").rstrip("/")
    key = (
        os.getenv("EVEN_ODD_SUPABASE_SECRET_KEY", "")
        or os.getenv("EVEN_ODD_SUPABASE_SERVICE_ROLE_KEY", "")
    )
    if not url or not key:
        raise EvenOddStorageError(
            "Dedicated Even/Odd database credentials are not configured."
        )
    return url, key


def storage_is_configured() -> bool:
    try:
        _configuration()
    except EvenOddStorageError:
        return False
    return True


def _headers(key: str, prefer: str | None = None) -> dict[str, str]:
    # Modern sb_secret_* keys are opaque, not JWTs. Sending one as a Bearer
    # token causes Supabase to reject it as an invalid JWT. Legacy service-role
    # keys are JWTs and continue to use both headers.
    headers = {
        "apikey": key,
        "Content-Type": "application/json",
    }
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    if prefer:
        headers["Prefer"] = prefer
    return headers


async def persist_collection(
    *,
    symbol: str,
    requested_ticks: int,
    ticks: list[dict[str, Any]],
) -> str:
    """Persist one collected sample and return its collection UUID.

    The collection record is created first. Tick rows are inserted in one
    bounded batch, then the collection is marked completed or partial.
    """
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            run_response = await client.post(
                f"{url}/rest/v1/even_odd_collection_runs",
                headers=_headers(key, "return=representation"),
                json={
                    "market_symbol": symbol,
                    "status": "collecting",
                    "requested_ticks": requested_ticks,
                    "received_ticks": 0,
                    "metadata": {"collector": "dedicated_deriv_public_websocket"},
                },
            )
            run_response.raise_for_status()
            run_rows = run_response.json()
            if not isinstance(run_rows, list) or not run_rows:
                raise EvenOddStorageError(
                    "Dedicated database did not return the new collection record."
                )
            collection_id = str(UUID(str(run_rows[0]["id"])))

            tick_rows = [
                {
                    "collection_id": collection_id,
                    "sequence_no": index,
                    "market_symbol": symbol,
                    "tick_epoch": tick["epoch"],
                    "quote": tick["quote"],
                    "pip_size": tick["pip_size"],
                    "last_digit": tick["last_digit"],
                    "parity": tick["parity"],
                    "deriv_tick_id": tick.get("tick_id"),
                    "received_at": tick["received_at"],
                }
                for index, tick in enumerate(ticks, start=1)
            ]

            if tick_rows:
                tick_response = await client.post(
                    f"{url}/rest/v1/even_odd_ticks",
                    headers=_headers(key, "return=minimal"),
                    json=tick_rows,
                )
                tick_response.raise_for_status()

            run_status = "completed" if len(ticks) == requested_ticks else "partial"
            finish_response = await client.patch(
                f"{url}/rest/v1/even_odd_collection_runs",
                params={"id": f"eq.{collection_id}"},
                headers=_headers(key, "return=minimal"),
                json={
                    "status": run_status,
                    "received_ticks": len(ticks),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            finish_response.raise_for_status()
            return collection_id
        except EvenOddStorageError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise EvenOddStorageError(
                "Failed to persist the collected sample in the dedicated Even/Odd database."
            ) from exc
