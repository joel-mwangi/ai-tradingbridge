"""Persistence adapter for the isolated Even/Odd Supabase project.

Only backend-side Even/Odd environment variables are accepted here. This
module deliberately never falls back to the application's primary Supabase
configuration.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx


class EvenOddStorageError(RuntimeError):
    """Raised when the dedicated Even/Odd database is unavailable."""


class EvenOddPolicyError(RuntimeError):
    """Raised when the database rejects a session/sequence policy transition."""


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
    *, symbol: str, requested_ticks: int, started_at: datetime,
    metadata: dict[str, Any] | None = None,
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
                    "metadata": {
                        "collector": "dedicated_deriv_public_websocket",
                        **(metadata or {}),
                    },
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
    capture_mode: str = "historical",
) -> None:
    """Persist the collected ticks and transition the run to a terminal state."""
    if status not in {"completed", "partial"}:
        raise ValueError("Collection terminal status must be completed or partial.")
    if capture_mode not in {"historical", "live_stream", "browser_stream", "all_live"}:
        raise ValueError("Unsupported tick capture mode.")
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
                    "capture_id": tick.get("capture_id") or str(uuid4()),
                    "collection_id": collection_id,
                    "sequence_no": index,
                    "market_symbol": symbol,
                    "tick_epoch": tick["epoch"],
                    "quote": tick["quote"],
                    "pip_size": tick["pip_size"],
                    "last_digit": tick["last_digit"],
                    "parity": tick["parity"],
                    "deriv_tick_id": tick.get("tick_id"),
                    "deriv_stream_id": tick.get("stream_id"),
                    "capture_mode": capture_mode,
                    "received_at": tick["received_at"],
                }
                for index, tick in enumerate(ticks, start=1)
            ]
            if tick_rows:
                tick_response = await client.post(
                    f"{url}/rest/v1/even_odd_ticks",
                    params={"on_conflict": "capture_id"},
                    headers=_headers(key, "resolution=ignore-duplicates,return=minimal"),
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
                received_ticks=len(ticks),
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


async def load_historical_ticks(
    *,
    symbol: str,
    limit: int = 5000,
    from_epoch: int | None = None,
    to_epoch: int | None = None,
    collection_id: str | None = None,
    capture_mode: str = "live_stream",
) -> dict[str, Any]:
    """Read the latest bounded history snapshot, returned in chronological order.

    The maximum observed row ID is captured first and used as a snapshot
    boundary, preventing concurrent collection inserts from shifting offset
    pages while this bounded read is in progress. Rows are fetched newest-first
    to avoid training on the oldest slice, then reversed before returning.
    """
    if not 1 <= limit <= 5000:
        raise ValueError("limit must be between 1 and 5000.")
    if capture_mode not in {"historical", "live_stream", "browser_stream"}:
        raise ValueError("Unsupported tick capture mode.")
    if from_epoch is not None and from_epoch <= 0:
        raise ValueError("from_epoch must be positive.")
    if to_epoch is not None and to_epoch <= 0:
        raise ValueError("to_epoch must be positive.")
    if from_epoch is not None and to_epoch is not None and from_epoch > to_epoch:
        raise ValueError("from_epoch cannot exceed to_epoch.")

    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    headers = _headers(key)
    page_size = 1000
    rows: list[dict[str, Any]] = []
    capture_filter = (
        "in.(live_stream,browser_stream)"
        if capture_mode == "all_live"
        else f"eq.{capture_mode}"
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            latest_response = await client.get(
                f"{url}/rest/v1/even_odd_ticks",
                params={
                    "select": "id",
                    "market_symbol": f"eq.{symbol}",
                    "capture_mode": capture_filter,
                    "order": "id.desc",
                    "limit": "1",
                },
                headers=headers,
            )
            latest_response.raise_for_status()
            latest_rows = latest_response.json()
            if not isinstance(latest_rows, list):
                raise EvenOddStorageError("Historical snapshot query returned an invalid response.")
            if not latest_rows:
                return {"rows": [], "snapshot_max_id": None}

            snapshot_max_id = int(latest_rows[0]["id"])
            base_params: dict[str, str] = {
                "select": (
                    "id,capture_id,collection_id,sequence_no,market_symbol,tick_epoch,quote,"
                    "pip_size,last_digit,parity,deriv_tick_id,deriv_stream_id,"
                    "capture_mode,received_at"
                ),
                "market_symbol": f"eq.{symbol}",
                "capture_mode": capture_filter,
                "id": f"lte.{snapshot_max_id}",
                "order": "tick_epoch.desc,id.desc",
            }
            if from_epoch is not None and to_epoch is not None:
                base_params["and"] = (
                    f"(tick_epoch.gte.{from_epoch},tick_epoch.lte.{to_epoch})"
                )
            elif from_epoch is not None:
                base_params["tick_epoch"] = f"gte.{from_epoch}"
            elif to_epoch is not None:
                base_params["tick_epoch"] = f"lte.{to_epoch}"
            if collection_id is not None:
                base_params["collection_id"] = f"eq.{collection_id}"

            while len(rows) < limit:
                page_start = len(rows)
                page_end = min(page_start + page_size, limit) - 1
                expected_page_size = page_end - page_start + 1
                response = await client.get(
                    f"{url}/rest/v1/even_odd_ticks",
                    params=base_params,
                    headers={
                        **headers,
                        "Range-Unit": "items",
                        "Range": f"{page_start}-{page_end}",
                    },
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list):
                    raise EvenOddStorageError(
                        "Historical tick query returned an invalid response."
                    )
                rows.extend(page)
                if len(page) < expected_page_size:
                    break
            return {
                "rows": list(reversed(rows[:limit])),
                "snapshot_max_id": snapshot_max_id,
            }
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError(
            "Could not load historical ticks from the dedicated database."
        ) from exc



async def persist_live_tick_batch(
    *, symbol: str, ticks: list[dict[str, Any]], worker_id: str,
    job_id: str | None = None,
) -> str | None:
    """Persist only observations received from the live Deriv subscription."""
    if not ticks:
        return None
    if len(ticks) > 300:
        raise ValueError("Live tick batch cannot exceed 300 observations.")
    requested = max(10, len(ticks))
    started_at = datetime.now(timezone.utc)
    collection_id = await start_collection_run(
        symbol=symbol,
        requested_ticks=requested,
        started_at=started_at,
        metadata={
            "capture_mode": "live_stream",
            "worker_id": worker_id,
            "batch_kind": "manual_job_batch" if job_id else "live_subscription",
            **({"job_id": job_id} if job_id else {}),
        },
    )
    await finish_collection_run(
        collection_id=collection_id,
        symbol=symbol,
        requested_ticks=requested,
        ticks=ticks,
        status="completed" if len(ticks) == requested else "partial",
        capture_mode="live_stream",
    )
    return collection_id


async def update_live_collector_heartbeat(
    *, symbol: str, worker_id: str, status: str,
    started_at: datetime, last_tick_at: datetime | None,
    ticks_received: int, last_error: str | None = None,
) -> None:
    """Upsert operational health for the independent live collector."""
    if status not in {"connecting", "connected", "reconnecting", "stopped", "error"}:
        raise ValueError("Invalid live collector status.")
    url, key = _configuration()
    payload = {
        "market_symbol": symbol,
        "worker_id": worker_id,
        "status": status,
        "started_at": started_at.isoformat(),
        "last_tick_at": last_tick_at.isoformat() if last_tick_at else None,
        "ticks_received": ticks_received,
        "last_error": last_error[:500] if last_error else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.post(
                f"{url}/rest/v1/even_odd_live_heartbeats",
                params={"on_conflict": "market_symbol"},
                headers=_headers(key, "resolution=merge-duplicates,return=minimal"),
                json=payload,
            )
            response.raise_for_status()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise EvenOddStorageError("Could not update live collector heartbeat.") from exc


async def get_live_collector_heartbeat(*, symbol: str) -> dict[str, Any] | None:
    """Read the latest live collector health record for a market."""
    url, key = _configuration()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            response = await client.get(
                f"{url}/rest/v1/even_odd_live_heartbeats",
                params={
                    "select": "market_symbol,worker_id,status,started_at,last_tick_at,ticks_received,last_error,updated_at",
                    "market_symbol": f"eq.{symbol}",
                    "limit": "1",
                },
                headers=_headers(key),
            )
            response.raise_for_status()
            rows = response.json()
            return rows[0] if isinstance(rows, list) and rows else None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise EvenOddStorageError("Could not read live collector heartbeat.") from exc


async def save_payout_quote_snapshot(*, quote: dict) -> None:
    """Persist a matched read-only Deriv proposal pair for later replay."""
    try:
        symbol = quote["symbol"]
        currency = quote["currency"]
        stake = quote["requested_stake"]
        quoted_at = quote["quoted_at"]
        even = quote["even"]
        odd = quote["odd"]
        even_spot_time = int(even["spot_time"])
        odd_spot_time = int(odd["spot_time"])
        if abs(even_spot_time - odd_spot_time) > 2:
            raise ValueError("Even and Odd quote spot times are not sufficiently close.")
        # Anchor the pair to the older proposal timestamp so replay never
        # treats the older side as if it had been quoted at a later instant.
        spot_time = min(even_spot_time, odd_spot_time)
        payload = {
            "market_symbol": symbol,
            "currency": currency,
            "stake": str(stake),
            "quoted_at": quoted_at,
            "spot_time": spot_time,
            "even_ask_price": str(even["ask_price"]),
            "even_payout": str(even["payout"]),
            "odd_ask_price": str(odd["ask_price"]),
            "odd_payout": str(odd["payout"]),
            "source": quote.get("source", "deriv_public_websocket"),
            "metadata": {
                "duration": quote.get("duration"),
                "duration_unit": quote.get("duration_unit"),
                "even_contract_type": even.get("contract_type"),
                "odd_contract_type": odd.get("contract_type"),
                "even_spot_time": even_spot_time,
                "odd_spot_time": odd_spot_time,
            },
        }
        url, key = _configuration()
        timeout = httpx.Timeout(15.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/rest/v1/even_odd_payout_quote_snapshots",
                params={"on_conflict": "market_symbol,currency,stake,spot_time"},
                headers=_headers(key, "resolution=ignore-duplicates,return=minimal"),
                json=payload,
            )
            response.raise_for_status()
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise EvenOddStorageError(
            "Could not persist the historical payout quote snapshot."
        ) from exc


async def load_payout_quote_snapshots(
    *,
    symbol: str,
    currency: str,
    stake: str,
    from_epoch: int | None = None,
    to_epoch: int | None = None,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """Load exact-contract quote snapshots with stable, bounded pagination."""
    if type(limit) is not int or not 1 <= limit <= 50000:
        raise ValueError("Payout snapshot limit must be from 1 to 50000.")
    if from_epoch is not None and (type(from_epoch) is not int or from_epoch <= 0):
        raise ValueError("from_epoch must be a positive integer.")
    if to_epoch is not None and (type(to_epoch) is not int or to_epoch <= 0):
        raise ValueError("to_epoch must be a positive integer.")
    if from_epoch is not None and to_epoch is not None and from_epoch > to_epoch:
        raise ValueError("from_epoch cannot exceed to_epoch.")

    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    headers = _headers(key)
    page_size = 1000
    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            latest_response = await client.get(
                f"{url}/rest/v1/even_odd_payout_quote_snapshots",
                params={"select": "id", "order": "id.desc", "limit": "1"},
                headers=headers,
            )
            latest_response.raise_for_status()
            latest_rows = latest_response.json()
            if not isinstance(latest_rows, list):
                raise EvenOddStorageError("Payout snapshot query returned an invalid response.")
            if not latest_rows:
                return []
            snapshot_max_id = int(latest_rows[0]["id"])

            params: dict[str, str] = {
                "select": (
                    "id,market_symbol,currency,stake,quoted_at,spot_time,"
                    "even_ask_price,even_payout,odd_ask_price,odd_payout,source,metadata"
                ),
                "market_symbol": f"eq.{symbol}",
                "currency": f"eq.{currency}",
                "stake": f"eq.{stake}",
                "id": f"lte.{snapshot_max_id}",
                "order": "spot_time.asc,id.asc",
            }
            if from_epoch is not None and to_epoch is not None:
                params["and"] = f"(spot_time.gte.{from_epoch},spot_time.lte.{to_epoch})"
            elif from_epoch is not None:
                params["spot_time"] = f"gte.{from_epoch}"
            elif to_epoch is not None:
                params["spot_time"] = f"lte.{to_epoch}"

            while len(rows) < limit:
                page_start = len(rows)
                page_end = min(page_start + page_size, limit) - 1
                expected_page_size = page_end - page_start + 1
                response = await client.get(
                    f"{url}/rest/v1/even_odd_payout_quote_snapshots",
                    params=params,
                    headers={
                        **headers,
                        "Range-Unit": "items",
                        "Range": f"{page_start}-{page_end}",
                    },
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list):
                    raise EvenOddStorageError("Payout snapshot query returned an invalid response.")
                rows.extend(page)
                if len(page) < expected_page_size:
                    break
            return rows[:limit]
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError(
            "Could not load historical payout quote snapshots."
        ) from exc


async def start_research_run(
    *,
    symbol: str,
    snapshot_max_id: int | None,
    dataset_rows: int,
    quality_report: dict[str, Any],
) -> str:
    """Create a durable research-run record before model fitting begins."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/rest/v1/even_odd_research_runs",
                headers=_headers(key, "return=representation"),
                json={
                    "market_symbol": symbol,
                    "snapshot_max_id": snapshot_max_id,
                    "dataset_rows": dataset_rows,
                    "status": "running",
                    "quality_report": quality_report,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                raise EvenOddStorageError("Research run record was not returned.")
            return str(UUID(str(rows[0]["id"])))
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError("Could not create the research run record.") from exc


async def save_candidate_model(
    *,
    symbol: str,
    model_version: str,
    model_artifact: dict[str, Any],
    metrics: dict[str, Any],
    snapshot_max_id: int,
    partition_sizes: dict[str, int],
) -> str:
    """Persist a trained model as a non-authorized candidate, never as validated."""
    required_partitions = ("train", "validation", "calibration", "test")
    if any(type(partition_sizes.get(name)) is not int or partition_sizes[name] <= 0
           for name in required_partitions):
        raise ValueError("All model research partitions must contain positive row counts.")
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/rest/v1/even_odd_model_registry",
                headers=_headers(key, "return=representation"),
                json={
                    "market_symbol": symbol,
                    "model_name": str(model_artifact.get("model_type", "unknown")),
                    "model_version": model_version,
                    "status": "candidate",
                    "execution_authorized": False,
                    "model_artifact": model_artifact,
                    "metrics": metrics,
                    "dataset_snapshot_max_id": snapshot_max_id,
                    "training_rows": partition_sizes["train"],
                    "validation_rows": partition_sizes["validation"],
                    "calibration_rows": partition_sizes["calibration"],
                    "test_rows": partition_sizes["test"],
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                raise EvenOddStorageError("Candidate model record was not returned.")
            return str(UUID(str(rows[0]["id"])))
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError("Could not persist the candidate model.") from exc


async def complete_research_run(
    *,
    research_run_id: str,
    model_id: str,
    partition_sizes: dict[str, int],
    evaluation: dict[str, Any],
) -> None:
    """Mark a research run completed only after its candidate model is saved."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.patch(
                f"{url}/rest/v1/even_odd_research_runs",
                params={"id": f"eq.{research_run_id}"},
                headers=_headers(key, "return=representation"),
                json={
                    "status": "completed",
                    "model_id": model_id,
                    "partition_sizes": partition_sizes,
                    "evaluation": evaluation,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                raise EvenOddStorageError("Research run completion did not update a row.")
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError("Could not complete the research run record.") from exc


async def fail_research_run(*, research_run_id: str) -> None:
    """Best-effort failure transition; never stores raw exception or credential data."""
    try:
        url, key = _configuration()
        timeout = httpx.Timeout(10.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.patch(
                f"{url}/rest/v1/even_odd_research_runs",
                params={"id": f"eq.{research_run_id}"},
                headers=_headers(key, "return=minimal"),
                json={
                    "status": "failed",
                    "error_message": "Research run failed. Inspect local research logs.",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            response.raise_for_status()
    except (httpx.HTTPError, ValueError, TypeError, EvenOddStorageError):
        return


async def create_even_odd_session(
    *,
    owner_user_id: str,
    symbol: str,
    account_mode: str,
    currency: str,
    fixed_stake: str,
    max_session_loss: str,
    max_trades_per_session: int = 10,
    max_consecutive_losses: int = 2,
) -> dict[str, Any]:
    """Create a persistent risk-bounded session; no broker order is submitted."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    payload = {
        "owner_user_id": owner_user_id,
        "market_symbol": symbol,
        "account_mode": account_mode,
        "currency": currency,
        "fixed_stake": fixed_stake,
        "max_session_loss": max_session_loss,
        "max_trades_per_session": max_trades_per_session,
        "max_consecutive_losses": max_consecutive_losses,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/rest/v1/even_odd_sessions",
                headers=_headers(key, "return=representation"),
                json=payload,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                raise EvenOddStorageError("Session creation did not return a record.")
            return rows[0]
    except EvenOddStorageError:
        raise
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            raise EvenOddPolicyError(
                "Session parameters were rejected by the dedicated database."
            ) from exc
        raise EvenOddStorageError("Could not create the Even/Odd session.") from exc
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError("Could not create the Even/Odd session.") from exc


async def start_even_odd_sequence(
    *,
    session_id: str,
    owner_user_id: str,
    model_id: str,
    model_version: str,
    planned_trades: int,
    expected_value_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Call the database's atomic, validated-model sequence-start function."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/rest/v1/rpc/even_odd_start_sequence",
                headers=_headers(key),
                json={
                    "p_session_id": session_id,
                    "p_owner_user_id": owner_user_id,
                    "p_model_id": model_id,
                    "p_model_version": model_version,
                    "p_planned_trades": planned_trades,
                    "p_expected_value_snapshot": expected_value_snapshot,
                },
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not result.get("sequence_id"):
                raise EvenOddStorageError("Sequence start did not return a valid result.")
            return result
    except EvenOddStorageError:
        raise
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            raise EvenOddPolicyError(
                "Sequence start was rejected by a session or model safety rule."
            ) from exc
        raise EvenOddStorageError("Could not start the Even/Odd sequence.") from exc
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError("Could not start the Even/Odd sequence.") from exc


async def record_even_odd_settlement(
    *,
    sequence_id: str,
    owner_user_id: str,
    trade_number: int,
    side: str,
    stake: str,
    profit_if_win: str,
    loss_if_lose: str,
    even_probability: str,
    lower_even_probability: str,
    upper_even_probability: str,
    conservative_expected_value: str,
    revalidation: dict[str, Any],
    outcome: str,
    realized_profit: str,
    external_contract_id: str | None = None,
) -> dict[str, Any]:
    """Atomically record a settled result and update sequence/session counters."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/rest/v1/rpc/even_odd_record_trade_settlement",
                headers=_headers(key),
                json={
                    "p_sequence_id": sequence_id,
                    "p_owner_user_id": owner_user_id,
                    "p_trade_number": trade_number,
                    "p_side": side,
                    "p_stake": stake,
                    "p_profit_if_win": profit_if_win,
                    "p_loss_if_lose": loss_if_lose,
                    "p_even_probability": even_probability,
                    "p_lower_even_probability": lower_even_probability,
                    "p_upper_even_probability": upper_even_probability,
                    "p_conservative_expected_value": conservative_expected_value,
                    "p_revalidation": revalidation,
                    "p_outcome": outcome,
                    "p_realized_profit": realized_profit,
                    "p_external_contract_id": external_contract_id,
                },
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or not result.get("trade_id"):
                raise EvenOddStorageError("Settlement did not return a valid result.")
            return result
    except EvenOddStorageError:
        raise
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            raise EvenOddPolicyError(
                "Settlement was rejected by a session, model, payout or risk rule."
            ) from exc
        raise EvenOddStorageError(
            "Could not record the Even/Odd trade settlement."
        ) from exc
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise EvenOddStorageError(
            "Could not record the Even/Odd trade settlement."
        ) from exc


async def get_even_odd_session_state(session_id: str, owner_user_id: str) -> dict[str, Any] | None:
    """Read one session and its sequences from the dedicated project."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            session_response = await client.get(
                f"{url}/rest/v1/even_odd_sessions",
                params={"select": "*", "id": f"eq.{session_id}", "owner_user_id": f"eq.{owner_user_id}", "limit": "1"},
                headers=_headers(key),
            )
            session_response.raise_for_status()
            sessions = session_response.json()
            if not isinstance(sessions, list):
                raise EvenOddStorageError("Session lookup returned an invalid response.")
            if not sessions:
                return None

            sequence_response = await client.get(
                f"{url}/rest/v1/even_odd_sequences",
                params={
                    "select": "*",
                    "session_id": f"eq.{session_id}",
                    "order": "created_at.desc",
                    "limit": "100",
                },
                headers=_headers(key),
            )
            sequence_response.raise_for_status()
            sequences = sequence_response.json()
            if not isinstance(sequences, list):
                raise EvenOddStorageError("Sequence lookup returned an invalid response.")
            return {"session": sessions[0], "sequences": sequences}
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise EvenOddStorageError("Could not load the Even/Odd session state.") from exc


async def get_even_odd_sequence_state(sequence_id: str, owner_user_id: str) -> dict[str, Any] | None:
    """Read one sequence and its settled trade history."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            sequence_response = await client.get(
                f"{url}/rest/v1/even_odd_sequences",
                params={"select": "*", "id": f"eq.{sequence_id}", "limit": "1"},
                headers=_headers(key),
            )
            sequence_response.raise_for_status()
            sequences = sequence_response.json()
            if not isinstance(sequences, list):
                raise EvenOddStorageError("Sequence lookup returned an invalid response.")
            if not sequences:
                return None

            owner_response = await client.get(
                f"{url}/rest/v1/even_odd_sessions",
                params={
                    "select": "id",
                    "id": f"eq.{sequences[0]['session_id']}",
                    "owner_user_id": f"eq.{owner_user_id}",
                    "limit": "1",
                },
                headers=_headers(key),
            )
            owner_response.raise_for_status()
            owned_sessions = owner_response.json()
            if not isinstance(owned_sessions, list):
                raise EvenOddStorageError("Sequence ownership lookup returned an invalid response.")
            if not owned_sessions:
                return None

            trade_response = await client.get(
                f"{url}/rest/v1/even_odd_trade_records",
                params={
                    "select": "*",
                    "sequence_id": f"eq.{sequence_id}",
                    "order": "trade_number.asc",
                    "limit": "7",
                },
                headers=_headers(key),
            )
            trade_response.raise_for_status()
            trades = trade_response.json()
            if not isinstance(trades, list):
                raise EvenOddStorageError("Trade lookup returned an invalid response.")
            return {"sequence": sequences[0], "trades": trades}
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise EvenOddStorageError("Could not load the Even/Odd sequence state.") from exc


async def list_even_odd_models(*, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
    """List model metadata and evaluation summaries without exposing artifacts."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{url}/rest/v1/even_odd_model_registry",
                params={
                    "select": (
                        "id,market_symbol,model_name,model_version,status,"
                        "execution_authorized,metrics,dataset_snapshot_max_id,"
                        "training_rows,validation_rows,calibration_rows,test_rows,"
                        "created_at,validated_at"
                    ),
                    "market_symbol": f"eq.{symbol}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                },
                headers=_headers(key),
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise EvenOddStorageError("Model registry returned an invalid response.")
            return rows
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise EvenOddStorageError("Could not list Even/Odd models.") from exc


async def list_even_odd_research_runs(
    *, symbol: str, limit: int = 50
) -> list[dict[str, Any]]:
    """List research-run summaries for one market."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{url}/rest/v1/even_odd_research_runs",
                params={
                    "select": (
                        "id,market_symbol,snapshot_max_id,dataset_rows,status,"
                        "quality_report,partition_sizes,evaluation,model_id,"
                        "started_at,completed_at,created_at,error_message"
                    ),
                    "market_symbol": f"eq.{symbol}",
                    "order": "created_at.desc",
                    "limit": str(limit),
                },
                headers=_headers(key),
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise EvenOddStorageError("Research-run history returned an invalid response.")
            return rows
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise EvenOddStorageError("Could not list Even/Odd research runs.") from exc


async def get_even_odd_model(model_id: str) -> dict[str, Any] | None:
    """Load one model artifact and its evaluation metadata for research forecasts."""
    url, key = _configuration()
    timeout = httpx.Timeout(20.0, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{url}/rest/v1/even_odd_model_registry",
                params={
                    "select": (
                        "id,market_symbol,model_name,model_version,status,"
                        "execution_authorized,model_artifact,metrics,created_at,validated_at"
                    ),
                    "id": f"eq.{model_id}",
                    "limit": "1",
                },
                headers=_headers(key),
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise EvenOddStorageError("Model lookup returned an invalid response.")
            return rows[0] if rows else None
    except EvenOddStorageError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise EvenOddStorageError("Could not load the Even/Odd model artifact.") from exc
