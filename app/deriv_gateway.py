"""Server-owned Deriv credential custody and account-session bootstrap.

Credentials are held in process memory only. This intentionally requires an
always-on ASGI deployment with one process until a shared encrypted session store
and distributed ownership/locking are implemented. Never log access tokens.
"""
from __future__ import annotations

import hmac
import os
import re
import time
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr, StrictStr

router = APIRouter(prefix="/deriv", tags=["deriv"])
bearer = HTTPBearer(auto_error=False)
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9]{2,32}$")
_credentials: dict[str, "Credential"] = {}


@dataclass
class Credential:
    access_token: str
    expires_at: float
    updated_at: float


class CredentialTransfer(BaseModel):
    user_id: StrictStr = Field(min_length=1, max_length=128)
    access_token: SecretStr = Field(min_length=1, max_length=4096)
    expires_in: int = Field(default=3600, ge=60, le=86400)


class AccountRequest(BaseModel):
    account_id: StrictStr = Field(min_length=2, max_length=32)


def _require_bridge_secret(supplied: str | None) -> None:
    expected = os.getenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Credential bridge is not configured.")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized credential transfer.")


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    """Validate the caller's Supabase access token with Supabase Auth."""
    token = credentials.credentials if credentials else ""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    anon_key = os.getenv("SUPABASE_ANON_KEY", "")
    if not token:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    if not supabase_url or not anon_key:
        raise HTTPException(status_code=503, detail="User authentication is not configured.")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{supabase_url}/auth/v1/user",
                headers={"apikey": anon_key, "Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid or expired app session.")
        payload = response.json()
        user_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise HTTPException(status_code=401, detail="Invalid app identity.")
        if not payload.get("email_confirmed_at"):
            raise HTTPException(status_code=403, detail="Verify your app email first.")
        return user_id
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Could not verify app session.")


@router.post("/internal/credentials", include_in_schema=False, status_code=204)
async def transfer_credentials(
    body: CredentialTransfer,
    x_tradebridge_bridge_token: Annotated[str | None, Header()] = None,
) -> None:
    """Trusted Next.js OAuth callback transfers a token; token is never returned."""
    _require_bridge_secret(x_tradebridge_bridge_token)
    # Replacing a user's credential invalidates their previous in-memory session.
    _credentials[body.user_id] = Credential(
        access_token=body.access_token.get_secret_value(),
        expires_at=time.time() + body.expires_in,
        updated_at=time.time(),
    )


@router.delete("/internal/credentials/{user_id}", include_in_schema=False, status_code=204)
async def revoke_transferred_credentials(
    user_id: str,
    x_tradebridge_bridge_token: Annotated[str | None, Header()] = None,
) -> None:
    _require_bridge_secret(x_tradebridge_bridge_token)
    _credentials.pop(user_id, None)


async def _owned_credential(user_id: str) -> Credential:
    credential = _credentials.get(user_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="Deriv is not connected to this backend session.")
    if credential.expires_at <= time.time():
        _credentials.pop(user_id, None)
        raise HTTPException(status_code=401, detail="Deriv authorization expired; reconnect Deriv.")
    return credential


@router.get("/session")
async def session_status(user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    """Return safe connection metadata, never credentials or provider WebSocket URLs."""
    credential = _credentials.get(user_id)
    if not credential or credential.expires_at <= time.time():
        return {"connected": False}
    return {
        "connected": True,
        "credential_updated_at": int(credential.updated_at),
        "expires_at": int(credential.expires_at),
    }


@router.post("/accounts")
async def list_accounts(
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    credential = await _owned_credential(user_id)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={
                    "Authorization": f"Bearer {credential.access_token}",
                    "Accept": "application/json",
                },
            )
        if response.status_code == 401:
            _credentials.pop(user_id, None)
            raise HTTPException(status_code=401, detail="Deriv authorization expired; reconnect.")
        if not response.is_success:
            raise HTTPException(status_code=502, detail="Deriv account lookup failed.")
        payload = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Deriv account lookup failed.")
    # Keep provider payload shape, but never return the OAuth token.
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Unexpected Deriv account response.")
    return payload


@router.post("/session/activate")
async def activate_account_session(
    body: AccountRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Validate account ownership/type and mint OTP server-side (URL is not exposed)."""
    if not ACCOUNT_ID_RE.fullmatch(body.account_id):
        raise HTTPException(status_code=422, detail="Invalid account identifier.")
    credential = await _owned_credential(user_id)

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            accounts_response = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
            )
            if accounts_response.status_code == 401:
                _credentials.pop(user_id, None)
                raise HTTPException(status_code=401, detail="Deriv authorization expired; reconnect.")
            if not accounts_response.is_success:
                raise HTTPException(status_code=502, detail="Deriv account verification failed.")
            accounts_payload = accounts_response.json()
            root = accounts_payload.get("data", accounts_payload) if isinstance(accounts_payload, dict) else {}
            accounts = root.get("accounts", []) if isinstance(root, dict) else []
            account = next((item for item in accounts if isinstance(item, dict) and item.get("account_id") == body.account_id), None)
            if account is None:
                raise HTTPException(status_code=403, detail="Account is not available to this Deriv authorization.")
            account_type = str(account.get("account_type", "")).lower()
            if account_type != "demo":
                raise HTTPException(
                    status_code=403,
                    detail="Real-account execution remains disabled until server-side risk limits and order reconciliation are enforced.",
                )
            otp_response = await client.post(
                f"https://api.derivws.com/trading/v1/options/accounts/{body.account_id}/otp",
                headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
            )
            if not otp_response.is_success:
                raise HTTPException(status_code=502, detail="Could not activate the demo trading session.")
            otp_payload = otp_response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Deriv session activation failed.")

    # Deliberately do not return the OTP URL. WebSocket ownership/proxying is
    # the next migration layer; returning it would still expose the provider
    # trading session to the browser and defeat the security boundary.
    if not isinstance(otp_payload, dict):
        raise HTTPException(status_code=502, detail="Unexpected Deriv session response.")
    data = otp_payload.get("data")
    provider_url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(provider_url, str) or not provider_url.startswith("wss://api.derivws.com/trading/v1/options/ws/demo?"):
        raise HTTPException(status_code=502, detail="Deriv returned an invalid demo session.")
    return {
        "active": False,
        "account_id": body.account_id,
        "account_type": "demo",
        "status": "provider_session_verified",
        "next_step": "server_websocket_proxy_not_yet_enabled",
    }


@router.delete("/session", status_code=204)
async def disconnect_deriv(user_id: Annotated[str, Depends(current_user)]) -> None:
    _credentials.pop(user_id, None)
