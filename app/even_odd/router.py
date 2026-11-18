"""Authenticated API for the independent Even/Odd Lab data domain."""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.even_odd.collector import TickCollectionError, collect_ticks
from app.even_odd.data_quality import inspect_historical_ticks
from app.even_odd.digit_engine import analyze_digits
from app.even_odd.schemas import (
    AnalyzeDigitsRequest,
    CollectTicksRequest,
    CreateSessionRequest,
    RecordTradeSettlementRequest,
    StartSequenceRequest,
)
from app.even_odd.storage import (
    EvenOddPolicyError,
    EvenOddStorageError,
    create_even_odd_session,
    fail_collection_run,
    finish_collection_run,
    load_historical_ticks,
    record_even_odd_settlement,
    start_collection_run,
    start_even_odd_sequence,
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
        "research_pipeline_available": True,
        "persistent_session_management_available": True,
        "order_execution_enabled": False,
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


@router.get("/history")
async def get_even_odd_history(
    _: Annotated[None, Depends(require_even_odd_api_token)],
    symbol: str = Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$"),
    limit: int = Query(default=5000, ge=1, le=5000),
    from_epoch: int | None = Query(default=None, ge=1),
    to_epoch: int | None = Query(default=None, ge=1),
    collection_id: UUID | None = None,
) -> dict:
    """Return a bounded, chronologically ordered snapshot of stored market ticks."""
    if from_epoch is not None and to_epoch is not None and from_epoch > to_epoch:
        raise HTTPException(
            status_code=422,
            detail="from_epoch cannot exceed to_epoch.",
        )
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        snapshot = await load_historical_ticks(
            symbol=symbol,
            limit=limit,
            from_epoch=from_epoch,
            to_epoch=to_epoch,
            collection_id=str(collection_id) if collection_id else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not load historical ticks from the dedicated database.",
        ) from exc

    rows = snapshot["rows"]
    return {
        "symbol": symbol,
        "returned_ticks": len(rows),
        "limit": limit,
        "snapshot_max_id": snapshot["snapshot_max_id"],
        "limit_reached": len(rows) == limit,
        "ordering": "tick_epoch_asc_then_id_asc",
        "rows": rows,
        "warning": (
            "Historical observations are research data, not proof of predictive "
            "edge. Overlapping collection windows may contain repeated market ticks."
        ),
    }



@router.get("/quality")
async def audit_even_odd_history(
    _: Annotated[None, Depends(require_even_odd_api_token)],
    symbol: str = Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$"),
    limit: int = Query(default=5000, ge=1, le=5000),
    from_epoch: int | None = Query(default=None, ge=1),
    to_epoch: int | None = Query(default=None, ge=1),
    collection_id: UUID | None = None,
) -> dict:
    """Audit structural integrity of a bounded historical market-data snapshot."""
    if from_epoch is not None and to_epoch is not None and from_epoch > to_epoch:
        raise HTTPException(
            status_code=422,
            detail="from_epoch cannot exceed to_epoch.",
        )
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        snapshot = await load_historical_ticks(
            symbol=symbol,
            limit=limit,
            from_epoch=from_epoch,
            to_epoch=to_epoch,
            collection_id=str(collection_id) if collection_id else None,
        )
        quality = inspect_historical_ticks(
            snapshot["rows"],
            expected_symbol=symbol,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not load historical ticks for quality auditing.",
        ) from exc

    return {
        "symbol": symbol,
        "snapshot_max_id": snapshot["snapshot_max_id"],
        "limit_reached": len(snapshot["rows"]) == limit,
        "quality": quality,
    }



@router.post("/sessions")
async def create_session(
    request: CreateSessionRequest,
    _: Annotated[None, Depends(require_even_odd_api_token)],
) -> dict:
    """Create a fixed-stake session; this endpoint does not place an order."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        session = await create_even_odd_session(
            symbol=request.symbol,
            account_mode=request.account_mode,
            currency=request.currency,
            fixed_stake=str(request.fixed_stake),
            max_session_loss=(
                str(request.max_session_loss)
                if request.max_session_loss is not None
                else None
            ),
            max_trades_per_session=request.max_trades_per_session,
            max_consecutive_losses=request.max_consecutive_losses,
        )
    except EvenOddPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not create session.") from exc

    return {
        "session": session,
        "order_execution_enabled": False,
    }


@router.post("/sequences")
async def start_sequence(
    request: StartSequenceRequest,
    _: Annotated[None, Depends(require_even_odd_api_token)],
) -> dict:
    """Start a sequence only when the database confirms a validated model."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        result = await start_even_odd_sequence(
            session_id=str(request.session_id),
            model_id=str(request.model_id),
            model_version=request.model_version,
            planned_trades=request.planned_trades,
            expected_value_snapshot=request.expected_value_snapshot,
        )
    except EvenOddPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not start sequence.") from exc

    return {
        **result,
        "order_execution_enabled": False,
    }


@router.post("/settlements")
async def record_trade_settlement(
    request: RecordTradeSettlementRequest,
    _: Annotated[None, Depends(require_even_odd_api_token)],
) -> dict:
    """Record a settled trade and atomically enforce sequence/session limits."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        result = await record_even_odd_settlement(
            sequence_id=str(request.sequence_id),
            trade_number=request.trade_number,
            side=request.side,
            stake=str(request.stake),
            profit_if_win=str(request.profit_if_win),
            loss_if_lose=str(request.loss_if_lose),
            even_probability=str(request.even_probability),
            lower_even_probability=str(request.lower_even_probability),
            upper_even_probability=str(request.upper_even_probability),
            conservative_expected_value=str(request.conservative_expected_value),
            revalidation=request.revalidation.model_dump(mode="json"),
            outcome=request.outcome,
            realized_profit=str(request.realized_profit),
            external_contract_id=request.external_contract_id,
        )
    except EvenOddPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not record settlement.") from exc

    return {
        **result,
        "order_execution_enabled": False,
    }
