import os
import re

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt

from app.analysis import analyze_digits

app = FastAPI(
    title="TradeBridge AI",
    description="Research and evaluation service for historical market data",
    version="0.2.0",
)

SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")


class DigitRequest(BaseModel):
    digits: list[StrictInt] = Field(min_length=1, max_length=100_000)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "tradebridge-ai",
        "mode": "research",
        "database_configured": bool(
            os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        ),
    }


@app.post("/analysis/digits")
def digit_analysis(request: DigitRequest):
    try:
        return analyze_digits(request.digits)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/analysis/digits/latest")
def analyze_latest_ticks(
    symbol: str = Query(default="1HZ100V", min_length=2, max_length=32),
    limit: int = Query(default=1000, ge=10, le=100_000),
):
    """Analyze stored ticks from Supabase; the service-role key stays server-side."""
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise HTTPException(status_code=422, detail="Invalid symbol format.")

    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured on this service.",
        )

    try:
        response = httpx.get(
            f"{supabase_url}/rest/v1/market_ticks",
            params={
                "select": "symbol,quote,epoch,pip_size,last_digit",
                "symbol": f"eq.{symbol}",
                "order": "epoch.desc",
                "limit": limit,
            },
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
            },
            timeout=15.0,
        )
        response.raise_for_status()
        rows = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Do not return upstream response bodies or credentials to callers.
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve tick history from Supabase.",
        ) from exc

    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Unexpected database response.")

    digits = []
    for row in rows:
        digit = row.get("last_digit") if isinstance(row, dict) else None
        if type(digit) is not int or not 0 <= digit <= 9:
            raise HTTPException(
                status_code=502,
                detail="Tick history contains an invalid last_digit value.",
            )
        digits.append(digit)

    if not digits:
        raise HTTPException(
            status_code=404,
            detail=f"No tick history found for symbol {symbol}.",
        )

    return {
        "symbol": symbol,
        "source": "supabase.market_ticks",
        "requested_limit": limit,
        "returned_ticks": len(digits),
        "analysis": analyze_digits(digits),
        "warning": (
            "Historical frequency analysis is not a trading signal and does "
            "not establish predictive performance."
        ),
    }
