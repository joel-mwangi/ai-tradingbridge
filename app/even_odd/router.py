"""Authenticated API for the independent Even/Odd Lab data domain."""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.even_odd.collector import TickCollectionError, collect_ticks
from app.even_odd.digit_engine import analyze_digits
from app.even_odd.schemas import AnalyzeDigitsRequest, CollectTicksRequest
from app.even_odd.storage import (
    EvenOddStorageError,
    fail_collection_run,
    finish_collection_run,
    start_collection_run,
    storage_is_configured,
)


router = APIRouter(prefix="/even-odd", tags=["even-odd-lab"])
bearer_scheme = HTTPBearer(auto_error=False)


def require_even_odd_api_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> None:
    """Use the backend's server-to-server token without coupling to app.main."""
    expected = os.getenv("TRADEBRIDGE_API_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured on this service.",
        )
    supplied = credentials.credentials if credentials else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/status")
def even_odd_status(
    _: Annotated[None, Depends(require_even_odd_api_token)],
) -> dict:
    return {
        "module": "even-odd-lab",
        "status": "available",
        "data_source": "dedicated_deriv_public_websocket",
        "storage_configured": storage_is_configured(),
        "execution_enabled": False,
        "predictive_model_available": False,
        "warning": "Descriptive market analysis only; no trading signal is produced.",
    }


@router.post("/analyze")
def analyze_even_odd_digits(
    request: AnalyzeDigitsRequest,
    _: Annotated[None, Depends(require_even_odd_api_token)],
) -> dict:
    try:
        result = analyze_digits(request.digits)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "source": "submitted_digit_sample",
        "returned_ticks": len(request.digits),
        "analysis": result,
    }


@router.post("/collect")
async def collect_even_odd_market_ticks(
    request: CollectTicksRequest,
    _: Annotated[None, Depends(require_even_odd_api_token)],
) -> dict:
    """Create an audit record, collect ticks, then persist a terminal run state."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )

    collection_started_at = datetime.now(timezone.utc)
    try:
        collection_id = await start_collection_run(
            symbol=request.symbol,
            requested_ticks=request.tick_count,
            started_at=collection_started_at,
        )
    except EvenOddStorageError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not create the collection audit record.",
        ) from exc

    try:
        ticks = await collect_ticks(
            symbol=request.symbol,
            tick_count=request.tick_count,
            timeout_seconds=request.timeout_seconds,
        )
    except ValueError as exc:
        await fail_collection_run(
            collection_id=collection_id,
            received_ticks=0,
            error_message="Invalid collection request.",
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TickCollectionError as exc:
        partial_ticks = exc.ticks
        if partial_ticks:
            try:
                await finish_collection_run(
                    collection_id=collection_id,
                    symbol=request.symbol,
                    requested_ticks=request.tick_count,
                    ticks=partial_ticks,
                    status="partial",
                    error_message=str(exc),
                )
            except EvenOddStorageError as storage_exc:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "Collection failed and partial ticks could not be persisted.",
                        "collection_id": collection_id,
                        "received_ticks": len(partial_ticks),
                    },
                ) from storage_exc
            raise HTTPException(
                status_code=502,
                detail={
                    "message": str(exc),
                    "collection_id": collection_id,
                    "status": "partial",
                    "received_ticks": len(partial_ticks),
                },
            ) from exc

        await fail_collection_run(
            collection_id=collection_id,
            received_ticks=0,
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=502,
            detail={
                "message": str(exc),
                "collection_id": collection_id,
                "status": "failed",
                "received_ticks": 0,
            },
        ) from exc

    try:
        await finish_collection_run(
            collection_id=collection_id,
            symbol=request.symbol,
            requested_ticks=request.tick_count,
            ticks=ticks,
            status="completed",
        )
    except EvenOddStorageError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Ticks were collected but could not be finalized in the dedicated database.",
                "collection_id": collection_id,
                "status": "failed_or_unconfirmed",
                "received_ticks": len(ticks),
            },
        ) from exc

    digits = [tick["last_digit"] for tick in ticks]
    return {
        "collection_id": collection_id,
        "symbol": request.symbol,
        "source": "dedicated_deriv_public_websocket",
        "storage": "dedicated_even_odd_supabase",
        "persisted": True,
        "requested_ticks": request.tick_count,
        "returned_ticks": len(ticks),
        "complete": True,
        "status": "completed",
        "ticks": ticks,
        "analysis": analyze_digits(digits),
        "predictive_edge_established": False,
        "warning": (
            "Observed frequencies and sequences are descriptive. This sample "
            "does not establish next-tick predictability or authorize a trade."
        ),
    }
