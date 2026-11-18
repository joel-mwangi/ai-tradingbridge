"""Persistence adapter for the isolated Even/Odd Supabase project.

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
    headers = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    if prefer:
        headers["Prefer"] = prefer
    return headers


async def _mark_collection_failed(
    client: httpx.AsyncClient,
    *,
    url: str,
    key: str,
    collection_id: str,
    received_ticks: int = 0,
    error_message: str = "Collection failed. Inspect backend logs for details.",
) -> None:
    """Best-effort terminal update that never masks the original failure."""
    try:
        response = await client.patch(
            f"{url}/rest/v1/even_odd_collection_runs",
            params={"id": f"eq.{collection_id}"},
            headers=_headers(key, "return=minimal"),
            json={
                "status": "failed",
                "received_ticks": received_ticks,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error_message": error_message[:500],
            },
        )
        response.raise_for_status()
    except (httpx.HTTPError, ValueError, TypeError):
        return


async def start_collection_run(
    *, symbol: str, requested_ticks: int, started_at: datetime
) -> str:
    """Create the durable audit record before opening the market-data socket."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/rest/v1/even_odd_collection_runs",
                headers=_headers(key, "return=representation"),
                json={
                    "market_symbol": symbol,
                    "status": "collecting",
                    "requested_ticks": requested_ticks,
                    "received_ticks": 0,
                    "started_at": started_at.isoformat(),
                    "metadata": {"collector": "dedicated_deriv_public_websocket"},
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                raise EvenOddStorageError(
                    "Dedicated database did not return the new collection record."
                )
            return str(UUID(str(rows[0]["id"])))
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError(
            "Could not create the collection audit record in the dedicated database."
        ) from exc


async def finish_collection_run(
    *,
    collection_id: str,
    symbol: str,
    requested_ticks: int,
    ticks: list[dict[str, Any]],
    status: str,
    error_message: str | None = None,
) -> None:
    """Persist the collected ticks and transition the run to a terminal state."""
    if status not in {"completed", "partial"}:
        raise ValueError("Collection terminal status must be completed or partial.")
    if len(ticks) > requested_ticks:
        raise ValueError("Received tick count cannot exceed requested tick count.")
    if status == "completed" and len(ticks) != requested_ticks:
        raise ValueError("A completed collection must contain every requested tick.")
    if status == "partial" and not 0 < len(ticks) < requested_ticks:
        raise ValueError("A partial collection must contain between 1 and requested ticks.")

    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
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
                    "deriv_stream_id": tick.get("stream_id"),
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

            finish_response = await client.patch(
                f"{url}/rest/v1/even_odd_collection_runs",
                params={"id": f"eq.{collection_id}"},
                headers=_headers(key, "return=minimal"),
                json={
                    "status": status,
                    "received_ticks": len(ticks),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error_message": error_message[:500] if error_message else None,
                },
            )
            finish_response.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            await _mark_collection_failed(
                client,
                url=url,
                key=key,
                collection_id=collection_id,
                received_ticks=0,
            )
            raise EvenOddStorageError(
                "Failed to persist or finalize the collection in the dedicated database."
            ) from exc


async def fail_collection_run(
    *, collection_id: str, received_ticks: int, error_message: str
) -> None:
    """Mark an upstream collection failure; database cleanup is best effort."""
    url, key = _configuration()
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await _mark_collection_failed(
            client,
            url=url,
            key=key,
            collection_id=collection_id,
            received_ticks=received_ticks,
            error_message=error_message,
        )


async def persist_collection(
    *,
    symbol: str,
    requested_ticks: int,
    ticks: list[dict[str, Any]],
    started_at: datetime,
) -> str:
    """Backward-compatible helper for tests and internal callers."""
    collection_id = await start_collection_run(
        symbol=symbol, requested_ticks=requested_ticks, started_at=started_at
    )
    status = "completed" if len(ticks) == requested_ticks else "partial"
    await finish_collection_run(
        collection_id=collection_id,
        symbol=symbol,
        requested_ticks=requested_ticks,
        ticks=ticks,
        status=status,
    )
    return collection_id
