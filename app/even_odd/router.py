"""Authenticated API for the independent Even/Odd Lab data domain."""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.even_odd.collector import TickCollectionError, collect_ticks
from app.even_odd.data_quality import inspect_historical_ticks
from app.even_odd.ml.features import build_next_tick_features
from app.even_odd.ml.model import predict_even_probability
from app.even_odd.ml.uncertainty import select_probability_interval
from app.even_odd.payouts import PayoutQuoteError, get_even_odd_payout_quote
from app.even_odd.policies import EvenOddPolicy
from app.even_odd.digit_engine import analyze_digits
from app.even_odd.entry_detector import (
    EntryObservation,
    EntryPolicy,
    evaluate_hybrid_entry,
)
from app.even_odd.schemas import (
    PlanSequenceRequest,
    AnalyzeDigitsRequest,
    CollectTicksRequest,
    CreateSessionRequest,
    EvaluateEntryRequest,
    RecordTradeSettlementRequest,
    StartSequenceRequest,
)
from app.even_odd.sequence_optimizer import CandidateTradeForecast, select_sequence_length
from app.even_odd.storage import (
    EvenOddPolicyError,
    EvenOddStorageError,
    create_even_odd_session,
    fail_collection_run,
    finish_collection_run,
    get_even_odd_model,
    get_even_odd_sequence_state,
    get_even_odd_session_state,
    load_historical_ticks,
    list_even_odd_models,
    list_even_odd_research_runs,
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


def require_even_odd_user_id(
    _: Annotated[None, Depends(require_even_odd_api_token)],
    user_id: Annotated[str | None, Header(alias="X-TradeBridge-User-ID")] = None,
) -> UUID:
    """Require a verified user context injected by the authenticated app proxy."""
    try:
        return UUID(user_id or "")
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid authenticated user context is required.",
        ) from exc


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
    owner_user_id: Annotated[UUID, Depends(require_even_odd_user_id)],
) -> dict:
    """Create a fixed-stake session; this endpoint does not place an order."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        session = await create_even_odd_session(
            owner_user_id=str(owner_user_id),
            symbol=request.symbol,
            account_mode=request.account_mode,
            currency=request.currency,
            fixed_stake=str(request.fixed_stake),
            max_session_loss=str(request.max_session_loss),
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
    owner_user_id: Annotated[UUID, Depends(require_even_odd_user_id)],
) -> dict:
    """Recompute the ML plan server-side, then atomically start that sequence."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    plan_request = PlanSequenceRequest(
        session_id=request.session_id,
        model_id=request.model_id,
        model_version=request.model_version,
        observations=request.observations,
    )
    # Never trust a client-provided planned_trades count or EV snapshot. The
    # same server-side planner used by /sequence-plan is rerun immediately
    # before the database transition.
    plan = await plan_adaptive_sequence(plan_request, owner_user_id)
    if plan.get("status") != "plan" or int(plan.get("planned_trades") or 0) < 1:
        return {
            **plan,
            "sequence_started": False,
            "order_execution_enabled": False,
        }

    expected_value_snapshot = {
        "plan_basis": plan.get("plan_basis"),
        "plan_reason": plan.get("plan_reason"),
        "expected_value_by_trade": plan.get("expected_value_by_trade", []),
        "conservative_ev_by_trade": plan.get("conservative_ev_by_trade", []),
        "forecasts": plan.get("forecasts", []),
        "payout_quote": plan.get("payout_quote"),
    }
    try:
        result = await start_even_odd_sequence(
            session_id=str(request.session_id),
            owner_user_id=str(owner_user_id),
            model_id=str(request.model_id),
            model_version=request.model_version,
            planned_trades=int(plan["planned_trades"]),
            expected_value_snapshot=expected_value_snapshot,
        )
    except EvenOddPolicyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not start sequence.") from exc

    return {
        **result,
        "sequence_plan": plan,
        "sequence_started": True,
        "order_execution_enabled": False,
    }


@router.post("/settlements")
async def record_trade_settlement(
    request: RecordTradeSettlementRequest,
    owner_user_id: Annotated[UUID, Depends(require_even_odd_user_id)],
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
            owner_user_id=str(owner_user_id),
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



@router.get("/sessions/{session_id}")
async def get_session_state(
    session_id: UUID,
    owner_user_id: Annotated[UUID, Depends(require_even_odd_user_id)],
) -> dict:
    """Read a persistent session and its sequence history."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        result = await get_even_odd_session_state(str(session_id), str(owner_user_id))
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not load session state.") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return result


@router.get("/sequences/{sequence_id}")
async def get_sequence_state(
    sequence_id: UUID,
    owner_user_id: Annotated[UUID, Depends(require_even_odd_user_id)],
) -> dict:
    """Read a sequence and its ordered settled trade records."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        result = await get_even_odd_sequence_state(str(sequence_id), str(owner_user_id))
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not load sequence state.") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Sequence not found.")
    return result



@router.get("/models")
async def list_models(
    _: Annotated[None, Depends(require_even_odd_api_token)],
    symbol: str = Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$"),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    """List candidate/validated model metadata without returning model artifacts."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        models = await list_even_odd_models(symbol=symbol, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not list models.") from exc
    return {"symbol": symbol, "count": len(models), "models": models}


@router.get("/research-runs")
async def list_research_runs(
    _: Annotated[None, Depends(require_even_odd_api_token)],
    symbol: str = Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$"),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    """List persisted research experiment summaries for one market."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        runs = await list_even_odd_research_runs(symbol=symbol, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not list research runs.") from exc
    return {"symbol": symbol, "count": len(runs), "research_runs": runs}



@router.get("/forecast")
async def forecast_next_parity(
    _: Annotated[None, Depends(require_even_odd_api_token)],
    symbol: str = Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$"),
    model_id: UUID = Query(),
    max_data_age_seconds: float = Query(default=5.0, ge=0.1, le=60.0),
) -> dict:
    """Produce a research-only next-parity forecast from the latest persisted ticks."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        model = await get_even_odd_model(str(model_id))
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found.")
        if model["market_symbol"] != symbol:
            raise HTTPException(status_code=422, detail="Model market does not match the requested symbol.")
        if model["status"] == "retired":
            raise HTTPException(status_code=409, detail="Retired models cannot produce forecasts.")

        artifact = model.get("model_artifact")
        if not isinstance(artifact, dict):
            raise EvenOddStorageError("Stored model artifact is invalid.")
        feature_names = artifact.get("feature_names")
        if not isinstance(feature_names, list):
            raise EvenOddStorageError("Stored model feature schema is invalid.")
        windows = sorted({
            int(name.split("_", 1)[0][1:])
            for name in feature_names
            if isinstance(name, str)
            and name.startswith("w")
            and name.endswith("_even_rate")
            and name.split("_", 1)[0][1:].isdigit()
        })
        if not windows:
            raise EvenOddStorageError("Stored model does not declare valid feature windows.")

        snapshot = await load_historical_ticks(
            symbol=symbol,
            limit=max(windows),
        )
        rows = snapshot["rows"]
        quality = inspect_historical_ticks(rows, expected_symbol=symbol)
        if (
            not quality["quality_passed"]
            or quality["exact_market_observation_duplicates"] > 0
            or len(rows) < max(windows)
        ):
            raise HTTPException(
                status_code=409,
                detail="Latest market-data snapshot is insufficient or failed quality checks.",
            )
        digits = [row["last_digit"] for row in rows]
        features = build_next_tick_features(digits, windows=tuple(windows))
        probability_even = predict_even_probability(artifact, features)

        latest_epoch = rows[-1]["tick_epoch"]
        now_epoch = datetime.now(timezone.utc).timestamp()
        data_age_seconds = max(0.0, now_epoch - latest_epoch)
        if latest_epoch > now_epoch + 2:
            raise HTTPException(
                status_code=409,
                detail="Latest market tick has a future timestamp.",
            )
        if data_age_seconds > max_data_age_seconds:
            raise HTTPException(
                status_code=409,
                detail="Latest market data is stale or unavailable.",
            )

        metrics = model.get("metrics") or {}
        walk_forward = metrics.get("walk_forward") or {}
        intervals = walk_forward.get("calibration_intervals") or []
        uncertainty = select_probability_interval(
            intervals,
            target_probability=probability_even,
        )
        model_validated = model.get("status") == "validated"
        uncertainty_validated = uncertainty.get("validated") is True
        return {
            "symbol": symbol,
            "model_id": model["id"],
            "model_version": model["model_version"],
            "model_status": model["status"],
            "model_validated": model_validated,
            "probability_even": probability_even,
            "probability_odd": 1 - probability_even,
            "lower_even_probability": uncertainty.get("lower_probability"),
            "upper_even_probability": uncertainty.get("upper_probability"),
            "uncertainty_method": uncertainty.get("method"),
            "uncertainty_level": uncertainty.get("confidence_level"),
            "uncertainty_validated": uncertainty_validated,
            "sample_size": len(rows),
            "latest_tick_epoch": latest_epoch,
            "latest_tick_row_id": rows[-1]["id"],
            "data_age_seconds": data_age_seconds,
            "data_quality": quality,
            "snapshot_max_id": snapshot["snapshot_max_id"],
            "entry_gate_prerequisites_passed": (
                model_validated and uncertainty_validated
                and data_age_seconds <= max_data_age_seconds
            ),
            "entry_candidate": False,
            "execution_authorized": False,
            "order_execution_enabled": False,
            "warning": (
                "Research forecast only. A point probability is not a trade signal; "
                "entry confirmation and independent validation gates still apply."
            ),
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not produce research forecast.") from exc



@router.get("/payouts")
async def get_payout_quotes(
    _: Annotated[None, Depends(require_even_odd_api_token)],
    symbol: str = Query(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$"),
    currency: str = Query(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$"),
    stake: Decimal = Query(gt=0),
) -> dict:
    """Fetch read-only current Deriv DIGITEVEN and DIGITODD proposal prices."""
    try:
        return await get_even_odd_payout_quote(
            symbol=symbol,
            currency=currency,
            stake=stake,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PayoutQuoteError as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve live Deriv payout quotes.") from exc


@router.post("/sequence-plan")
async def plan_adaptive_sequence(
    request: PlanSequenceRequest,
    owner_user_id: Annotated[UUID, Depends(require_even_odd_user_id)],
) -> dict:
    """Plan a 1–7 trade upper bound from server-recomputed recent opportunities."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    try:
        model_id = str(request.model_id)
        model = await get_even_odd_model(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found.")
        if model.get("model_version") != request.model_version:
            raise HTTPException(status_code=409, detail="Model version is no longer current.")
        if model.get("status") != "validated":
            return {
                "status": "wait",
                "planned_trades": 0,
                "reason": "Sequence planning requires an independently validated model.",
                "execution_authorized": False,
                "order_execution_enabled": False,
            }

        session_state = await get_even_odd_session_state(str(request.session_id), str(owner_user_id))
        if session_state is None:
            raise HTTPException(status_code=404, detail="Even/Odd session not found.")
        session = session_state["session"]
        if session.get("status") != "active":
            raise HTTPException(status_code=409, detail="Sequence planning requires an active session.")
        if session.get("market_symbol") != model.get("market_symbol"):
            raise HTTPException(status_code=409, detail="Session and model markets do not match.")
        if any(
            sequence.get("status") == "active"
            for sequence in session_state.get("sequences", [])
        ):
            raise HTTPException(status_code=409, detail="An active sequence already exists for this session.")
        if int(session.get("completed_trades") or 0) >= int(session.get("max_trades_per_session") or 10):
            raise HTTPException(status_code=409, detail="Session trade limit has been reached.")
        if int(session.get("consecutive_losses") or 0) >= int(session.get("max_consecutive_losses") or 2):
            raise HTTPException(status_code=409, detail="Session has reached its consecutive-loss stop.")

        fixed_stake = Decimal(str(session["fixed_stake"]))
        payout_quote = await get_even_odd_payout_quote(
            symbol=model["market_symbol"],
            currency=str(session["currency"]),
            stake=fixed_stake,
        )
        for side in ("even", "odd"):
            if abs(Decimal(payout_quote[side]["ask_price"]) - fixed_stake) > Decimal("0.000001"):
                raise PayoutQuoteError("Deriv quote stake does not match the session fixed stake.")

        remaining_loss_budget = (
            Decimal(str(session["max_session_loss"]))
            + Decimal(str(session.get("realized_profit") or "0"))
        )
        required_loss_budget = max(
            Decimal(payout_quote["even"]["loss_if_lose"]),
            Decimal(payout_quote["odd"]["loss_if_lose"]),
        )
        risk_budget_available = remaining_loss_budget >= required_loss_budget
        if not risk_budget_available:
            return {
                "status": "wait",
                "planned_trades": 0,
                "reason": "The remaining session loss budget cannot cover the quoted stake.",
                "payout_quote": payout_quote,
                "execution_authorized": False,
                "order_execution_enabled": False,
            }

        artifact = model.get("model_artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("feature_names"), list):
            raise EvenOddStorageError("Stored model feature schema is invalid.")
        windows = sorted({
            int(name.split("_", 1)[0][1:])
            for name in artifact["feature_names"]
            if isinstance(name, str)
            and name.startswith("w")
            and name.endswith("_even_rate")
            and name.split("_", 1)[0][1:].isdigit()
        })
        if not windows:
            raise EvenOddStorageError("Stored model does not declare valid feature windows.")

        snapshot = await load_historical_ticks(symbol=model["market_symbol"], limit=5000)
        market_rows = snapshot["rows"]
        quality = inspect_historical_ticks(market_rows, expected_symbol=model["market_symbol"])
        if (
            not quality["quality_passed"]
            or quality["exact_market_observation_duplicates"] > 0
            or len(market_rows) < max(windows)
        ):
            raise HTTPException(
                status_code=409,
                detail="Persisted market history is insufficient or failed quality checks.",
            )
        rows_by_id = {row["id"]: row for row in market_rows}
        row_positions = {row["id"]: index for index, row in enumerate(market_rows)}
        now_epoch = datetime.now(timezone.utc).timestamp()
        metrics = model.get("metrics") or {}
        walk_forward = metrics.get("walk_forward") or {}
        calibration_intervals = walk_forward.get("calibration_intervals") or []

        observations: list[EntryObservation] = []
        entry_policy = EntryPolicy(minimum_sample_size=max(windows))
        forecasts: list[CandidateTradeForecast] = []
        forecast_details = []
        previous_id = 0
        previous_epoch = 0
        previous_position = None
        even_quote = payout_quote["even"]
        odd_quote = payout_quote["odd"]

        for trade_number, item in enumerate(request.observations, start=1):
            persisted_tick = rows_by_id.get(item.tick_row_id)
            if (
                persisted_tick is None
                or persisted_tick["tick_epoch"] != item.tick_epoch
            ):
                raise HTTPException(status_code=409, detail="Sequence observation is not a persisted market tick.")
            if item.tick_row_id <= previous_id or item.tick_epoch < previous_epoch:
                raise HTTPException(status_code=409, detail="Sequence observations are not chronologically ordered.")
            if item.tick_epoch > now_epoch + 2:
                raise HTTPException(status_code=409, detail="Sequence observation is future-dated.")
            previous_id = item.tick_row_id
            previous_epoch = item.tick_epoch
            current_position = row_positions[item.tick_row_id]
            if previous_position is not None and current_position != previous_position + 1:
                raise HTTPException(
                    status_code=409,
                    detail="Sequence observations must reference consecutive persisted market ticks.",
                )
            previous_position = current_position

            data_age_seconds = max(0.0, now_epoch - item.tick_epoch)
            target_index = current_position
            prior_rows = market_rows[:target_index]
            if len(prior_rows) < max(windows):
                raise HTTPException(status_code=409, detail="Not enough history precedes a sequence observation.")
            features = build_next_tick_features(
                [row["last_digit"] for row in prior_rows],
                windows=tuple(windows),
            )
            probability_even = predict_even_probability(artifact, features)
            uncertainty = select_probability_interval(
                calibration_intervals,
                target_probability=probability_even,
            )
            uncertainty_validated = uncertainty.get("validated") is True
            lower_even = float(uncertainty["lower_probability"]) if uncertainty_validated else 0.0
            upper_even = float(uncertainty["upper_probability"]) if uncertainty_validated else 1.0
            lower_odd = 1.0 - upper_even

            side_candidates = []
            for side, quote, probability, lower_probability in (
                ("even", even_quote, probability_even, lower_even),
                ("odd", odd_quote, 1.0 - probability_even, lower_odd),
            ):
                profit = float(quote["profit_if_win"])
                loss = float(quote["loss_if_lose"])
                break_even = loss / (profit + loss)
                conservative_ev = lower_probability * profit - (1 - lower_probability) * loss
                edge = lower_probability - break_even
                side_candidates.append({
                    "side": side,
                    "win_probability": probability,
                    "lower_bound_win_probability": lower_probability,
                    "profit_if_win": profit,
                    "loss_if_lose": loss,
                    "break_even_probability": break_even,
                    "conservative_expected_value": conservative_ev,
                    "edge_over_break_even": edge,
                })

            selected_side = max(
                side_candidates,
                key=lambda candidate: candidate["conservative_expected_value"],
            )
            signal_valid = (
                uncertainty_validated
                and data_age_seconds <= entry_policy.maximum_data_age_seconds
                and selected_side["edge_over_break_even"] >= entry_policy.minimum_edge_over_break_even
                and selected_side["conservative_expected_value"] > entry_policy.minimum_conservative_expected_value
            )
            forecasts.append(CandidateTradeForecast(
                trade_number=trade_number,
                win_probability=selected_side["win_probability"],
                lower_bound_win_probability=selected_side["lower_bound_win_probability"],
                profit_if_win=selected_side["profit_if_win"],
                loss_if_lose=selected_side["loss_if_lose"],
                signal_valid=signal_valid,
                model_validated=True,
                uncertainty_validated=uncertainty_validated,
                risk_budget_available=risk_budget_available,
                model_id=model_id,
                model_version=request.model_version,
            ))
            observations.append(EntryObservation(
                tick_row_id=persisted_tick["id"],
                tick_epoch=persisted_tick["tick_epoch"],
                sample_size=max(windows),
                data_age_seconds=data_age_seconds,
                even_probability=probability_even,
                lower_even_probability=lower_even,
                upper_even_probability=upper_even,
                even_profit_if_win=float(even_quote["profit_if_win"]),
                even_loss_if_lose=float(even_quote["loss_if_lose"]),
                odd_profit_if_win=float(odd_quote["profit_if_win"]),
                odd_loss_if_lose=float(odd_quote["loss_if_lose"]),
                model_id=model_id,
                model_version=request.model_version,
                model_validated=True,
                uncertainty_method=uncertainty.get("method") or "unavailable",
                uncertainty_level=float(uncertainty.get("confidence_level") or 0.9),
                uncertainty_validated=uncertainty_validated,
                risk_budget_available=risk_budget_available,
            ))
            forecast_details.append({
                "trade_number": trade_number,
                "tick_row_id": persisted_tick["id"],
                "tick_epoch": persisted_tick["tick_epoch"],
                "data_age_seconds": data_age_seconds,
                "side": selected_side["side"],
                "win_probability": selected_side["win_probability"],
                "lower_bound_win_probability": selected_side["lower_bound_win_probability"],
                "break_even_probability": selected_side["break_even_probability"],
                "conservative_expected_value": selected_side["conservative_expected_value"],
                "edge_over_break_even": selected_side["edge_over_break_even"],
                "uncertainty_validated": uncertainty_validated,
                "signal_valid": signal_valid,
            })

        if len(observations) < 3:
            raise HTTPException(status_code=422, detail="At least three recent observations are required for entry confirmation.")
        entry_result = evaluate_hybrid_entry(
            observations[-3:],
            policy=entry_policy,
        )
        if entry_result["status"] != "candidate":
            return {
                "status": "wait",
                "planned_trades": 0,
                "reason": entry_result["reason"],
                "entry_check": entry_result,
                "forecasts": forecast_details,
                "payout_quote": payout_quote,
                "execution_authorized": False,
                "order_execution_enabled": False,
            }

        remaining_session_trades = (
            int(session.get("max_trades_per_session") or 10)
            - int(session.get("completed_trades") or 0)
        )
        decision = select_sequence_length(
            forecasts,
            policy=EvenOddPolicy(),
            session_trades_remaining=remaining_session_trades,
        )
        return {
            "status": "plan" if decision.approved else "wait",
            "planned_trades": decision.selected_trades,
            "max_allowed_trades": decision.max_allowed_trades,
            "expected_value_by_trade": list(decision.expected_value_by_trade),
            "conservative_ev_by_trade": list(decision.conservative_ev_by_trade),
            "model_id": model_id,
            "model_version": request.model_version,
            "session_id": str(request.session_id),
            "plan_basis": "recent_persisted_opportunity_persistence_proxy",
            "plan_reason": decision.reason,
            "forecasts": forecast_details,
            "entry_check": entry_result,
            "payout_quote": payout_quote,
            "warning": (
                "This plan applies the current payout quote to recent persisted "
                "opportunities as a persistence proxy; it is not a historical payout "
                "replay or a trained forward sequence-survival forecast. It is an "
                "upper bound; revalidate after every completed trade. No order was placed."
            ),
            "execution_authorized": False,
            "order_execution_enabled": False,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PayoutQuoteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not verify sequence planning evidence.") from exc


@router.post("/entry-check")
async def check_hybrid_entry(
    request: EvaluateEntryRequest,
    owner_user_id: Annotated[UUID, Depends(require_even_odd_user_id)],
) -> dict:
    """Evaluate consecutive evidence against model and uncertainty records in Supabase."""
    if not storage_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Dedicated Even/Odd database is not configured on this service.",
        )
    model_id = str(request.observations[0].model_id)
    model_version = request.observations[0].model_version
    if any(
        str(item.model_id) != model_id or item.model_version != model_version
        for item in request.observations
    ):
        raise HTTPException(
            status_code=409,
            detail="All entry observations must reference the same model version.",
        )
    try:
        model = await get_even_odd_model(model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Model not found.")
        if model.get("model_version") != model_version:
            raise HTTPException(status_code=409, detail="Model version is no longer current.")
        if model.get("status") == "retired":
            raise HTTPException(status_code=409, detail="Retired models cannot authorize entry checks.")
        if model.get("status") != "validated":
            return {
                "status": "wait",
                "reason": "The selected model has not passed independent validation.",
                "model_status": model.get("status"),
                "execution_authorized": False,
                "order_execution_enabled": False,
            }

        session_state = await get_even_odd_session_state(str(request.session_id), str(owner_user_id))
        if session_state is None:
            raise HTTPException(status_code=404, detail="Even/Odd session not found.")
        session = session_state["session"]
        if session.get("status") != "active":
            raise HTTPException(status_code=409, detail="Entry checks require an active session.")
        if session.get("market_symbol") != model.get("market_symbol"):
            raise HTTPException(status_code=409, detail="Session and model markets do not match.")
        if session.get("max_session_loss") is None:
            raise HTTPException(status_code=409, detail="Session has no configured monetary loss cap.")
        if int(session.get("completed_trades") or 0) >= int(session.get("max_trades_per_session") or 10):
            raise HTTPException(status_code=409, detail="Session trade limit has been reached.")
        if int(session.get("consecutive_losses") or 0) >= int(session.get("max_consecutive_losses") or 2):
            raise HTTPException(status_code=409, detail="Session has reached its consecutive-loss stop.")

        fixed_stake = Decimal(str(session["fixed_stake"]))
        currency = str(session["currency"])
        payout_quote = await get_even_odd_payout_quote(
            symbol=model["market_symbol"],
            currency=currency,
            stake=fixed_stake,
        )
        for side in ("even", "odd"):
            quoted_stake = Decimal(payout_quote[side]["ask_price"])
            if abs(quoted_stake - fixed_stake) > Decimal("0.000001"):
                raise PayoutQuoteError("Deriv quote stake does not match the session fixed stake.")

        remaining_loss_budget = (
            Decimal(str(session["max_session_loss"]))
            + Decimal(str(session.get("realized_profit") or "0"))
        )
        required_loss_budget = max(
            Decimal(payout_quote["even"]["loss_if_lose"]),
            Decimal(payout_quote["odd"]["loss_if_lose"]),
        )
        if remaining_loss_budget < required_loss_budget:
            return {
                "status": "wait",
                "reason": "The session monetary loss budget cannot cover the quoted stake.",
                "session_id": str(request.session_id),
                "payout_quote": payout_quote,
                "execution_authorized": False,
                "order_execution_enabled": False,
            }
        risk_budget_available = True

        artifact = model.get("model_artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("feature_names"), list):
            raise EvenOddStorageError("Stored model feature schema is invalid.")
        windows = sorted({
            int(name.split("_", 1)[0][1:])
            for name in artifact["feature_names"]
            if isinstance(name, str)
            and name.startswith("w")
            and name.endswith("_even_rate")
            and name.split("_", 1)[0][1:].isdigit()
        })
        if not windows:
            raise EvenOddStorageError("Stored model does not declare valid feature windows.")

        snapshot = await load_historical_ticks(
            symbol=model["market_symbol"],
            limit=5000,
        )
        market_rows = snapshot["rows"]
        quality = inspect_historical_ticks(
            market_rows,
            expected_symbol=model["market_symbol"],
        )
        if (
            not quality["quality_passed"]
            or quality["exact_market_observation_duplicates"] > 0
            or len(market_rows) < max(windows)
        ):
            raise HTTPException(
                status_code=409,
                detail="Persisted market history is insufficient or failed quality checks.",
            )
        rows_by_id = {row["id"]: row for row in market_rows}
        row_positions = {row["id"]: index for index, row in enumerate(market_rows)}
        now_epoch = datetime.now(timezone.utc).timestamp()

        metrics = model.get("metrics") or {}
        walk_forward = metrics.get("walk_forward") or {}
        calibration_intervals = walk_forward.get("calibration_intervals") or []
        model_validated = model.get("status") == "validated"

        observations = []
        previous_position = None
        for item in request.observations:
            persisted_tick = rows_by_id.get(item.tick_row_id)
            if (
                persisted_tick is None
                or persisted_tick["tick_epoch"] != item.tick_epoch
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Entry observation does not match a persisted market tick.",
                )
            if persisted_tick["tick_epoch"] > now_epoch + 2:
                raise HTTPException(
                    status_code=409,
                    detail="Entry observation contains a future-dated market tick.",
                )
            current_position = row_positions[item.tick_row_id]
            if previous_position is not None and current_position != previous_position + 1:
                raise HTTPException(
                    status_code=409,
                    detail="Entry confirmations must reference consecutive persisted market ticks.",
                )
            previous_position = current_position
            data_age_seconds = max(
                0.0,
                now_epoch - persisted_tick["tick_epoch"],
            )
            target_index = current_position
            prior_rows = market_rows[:target_index]
            if len(prior_rows) < max(windows):
                raise HTTPException(
                    status_code=409,
                    detail="Not enough persisted ticks precede an entry observation.",
                )
            prior_digits = [row["last_digit"] for row in prior_rows]
            features = build_next_tick_features(prior_digits, windows=tuple(windows))
            probability_even = predict_even_probability(artifact, features)
            uncertainty = select_probability_interval(
                calibration_intervals,
                target_probability=probability_even,
            )
            uncertainty_validated = uncertainty.get("validated") is True
            observations.append(
                EntryObservation(
                    tick_row_id=persisted_tick["id"],
                    tick_epoch=persisted_tick["tick_epoch"],
                    sample_size=max(windows),
                    data_age_seconds=float(data_age_seconds),
                    even_probability=probability_even,
                    lower_even_probability=(
                        float(uncertainty["lower_probability"])
                        if uncertainty_validated else 0.0
                    ),
                    upper_even_probability=(
                        float(uncertainty["upper_probability"])
                        if uncertainty_validated else 1.0
                    ),
                    even_profit_if_win=float(payout_quote["even"]["profit_if_win"]),
                    even_loss_if_lose=float(payout_quote["even"]["loss_if_lose"]),
                    odd_profit_if_win=float(payout_quote["odd"]["profit_if_win"]),
                    odd_loss_if_lose=float(payout_quote["odd"]["loss_if_lose"]),
                    model_id=model_id,
                    model_version=model_version,
                    model_validated=model_validated,
                    uncertainty_method=(
                        uncertainty.get("method") or "unavailable"
                    ),
                    uncertainty_level=float(
                        uncertainty.get("confidence_level") or 0.9
                    ),
                    uncertainty_validated=uncertainty_validated,
                    risk_budget_available=risk_budget_available,
                )
            )

        # Entry thresholds are server-owned. The required history sample is
        # tied to the model's largest trained feature window; callers cannot
        # weaken freshness, confirmation, edge, or expected-value gates.
        policy = EntryPolicy(minimum_sample_size=max(windows))
        result = evaluate_hybrid_entry(observations, policy=policy)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PayoutQuoteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except EvenOddStorageError as exc:
        raise HTTPException(status_code=502, detail="Could not verify model entry evidence.") from exc

    return {
        **result,
        "session_id": str(request.session_id),
        "model_status": model["status"],
        "model_probability_even": observations[-1].even_probability,
        "latest_observation_sample_size": observations[-1].sample_size,
        "latest_observation_data_age_seconds": observations[-1].data_age_seconds,
        "payout_quote": payout_quote,
        "payout_terms_source": payout_quote["source"],
        "execution_authorized": False,
        "order_execution_enabled": False,
    }
