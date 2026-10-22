import hmac
import os
import re
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StrictInt

from app.analysis import analyze_digits
from app.deriv_gateway import router as deriv_router
from app.ctrader_gateway import router as ctrader_router




app = FastAPI(
    title="TradeBridge AI",
    description="Authenticated Deriv session gateway and descriptive research API",
    version="0.4.0",
)

app.include_router(deriv_router)
app.include_router(ctrader_router)

# Explicitly allow only configured frontend origins for browser HTTP calls.
# Set TRADEBRIDGE_ALLOWED_ORIGINS to comma-separated exact origins, e.g.
# https://swigtrade.vercel.app,http://localhost:3000
_allowed_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv("TRADEBRIDGE_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")
bearer_scheme = HTTPBearer(auto_error=False)


class DigitRequest(BaseModel):
    digits: list[StrictInt] = Field(min_length=1, max_length=100_000)


def require_api_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> None:
    """Require server-to-server bearer auth; fail closed if deployment is misconfigured."""
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

@app.get("/")
def home():
    return {"status": "ok"}




@app.get("/health")
def health():
    # Public liveness endpoint: do not disclose whether secrets are configured.
    return {"status": "ok", "service": "tradebridge-backend", "mode": "backend"}


@app.post("/analysis/digits")
def digit_analysis(
    request: DigitRequest,
    _: Annotated[None, Depends(require_api_token)],
):
    try:
        return analyze_digits(request.digits)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/analysis/digits/latest")
def analyze_latest_ticks(
    _: Annotated[None, Depends(require_api_token)],
    symbol: str = Query(default="1HZ100V", min_length=2, max_length=32),
    limit: int = Query(default=1000, ge=10, le=10_000),
):
    """Analyze stored ticks from Supabase; credentials remain server-side."""
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
            timeout=10.0,
        )
        response.raise_for_status()
        rows = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Unable to retrieve tick history from Supabase.",
        ) from exc

    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Unexpected database response.")

    digits = []
    for row in rows:
        if not isinstance(row, dict):
            raise HTTPException(status_code=502, detail="Malformed tick history row.")
        digit = row.get("last_digit")
        if type(digit) is not int or not 0 <= digit <= 9:
            raise HTTPException(
                status_code=502,
                detail="Tick history contains an invalid last_digit value.",
            )
        if row.get("symbol") != symbol:
            raise HTTPException(
                status_code=502,
                detail="Tick history contains a row for an unexpected symbol.",
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
