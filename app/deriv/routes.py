"""HTTP route handlers for the Deriv gateway (OAuth, credentials, sessions)."""
from __future__ import annotations

import hmac
import math
import os
import time
from typing import Annotated, Any

import httpx
from fastapi import Depends, Header, HTTPException

from app.deriv import router
import app.deriv.credentials as _creds
from app.deriv.credentials import _linked_account_ids, _owned_credential
from app.deriv.models import (
    ACCOUNT_ID_RE,
    AccountOwnershipRequest,
    AccountRequest,
    CredentialTransfer,
    OAuthExchangeRequest,
)
from app.deriv.security import _require_bridge_secret, current_user


@router.post("/internal/oauth/exchange", include_in_schema=False)
async def exchange_oauth_code(
    body: OAuthExchangeRequest,
    x_tradebridge_bridge_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Exchange Deriv OAuth code on FastAPI and retain the token only server-side."""
    _require_bridge_secret(x_tradebridge_bridge_token)
    client_id = os.getenv("DERIV_OAUTH_CLIENT_ID", "").strip()
    configured_redirect = os.getenv("DERIV_OAUTH_REDIRECT_URI", "").strip()
    if not configured_redirect:
        app_url = os.getenv("APP_URL", "").strip().rstrip("/")
        configured_redirect = f"{app_url}/api/auth/deriv/callback" if app_url else ""
    if not client_id or not configured_redirect:
        raise HTTPException(status_code=503, detail="Deriv OAuth is not configured on the backend.")
    if not hmac.compare_digest(body.redirect_uri, configured_redirect):
        raise HTTPException(status_code=400, detail="OAuth redirect URI does not match backend configuration.")

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            token_response = await client.post(
                "https://auth.deriv.com/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": body.code,
                    "code_verifier": body.code_verifier,
                    "redirect_uri": configured_redirect,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            if not token_response.is_success:
                raise HTTPException(status_code=502, detail="Deriv OAuth code exchange failed.")
            token_payload = token_response.json()
            access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
            if not isinstance(access_token, str) or not access_token:
                raise HTTPException(status_code=502, detail="Deriv returned an invalid OAuth response.")
            expires_raw = token_payload.get("expires_in", 3600)
            try:
                expires_in = max(1, min(int(expires_raw), 86400))
            except (TypeError, ValueError):
                expires_in = 3600

            accounts_response = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if accounts_response.status_code == 401:
                raise HTTPException(status_code=502, detail="Deriv rejected the new OAuth credential.")
            if not accounts_response.is_success:
                raise HTTPException(status_code=502, detail="Deriv account verification failed.")
            accounts_payload = accounts_response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Deriv OAuth verification failed.")

    root = accounts_payload if isinstance(accounts_payload, dict) else {}
    data = root.get("data")
    data_record = data if isinstance(data, dict) else {}
    candidates = (
        data if isinstance(data, list)
        else data_record.get("accounts") if isinstance(data_record.get("accounts"), list)
        else [data_record] if data_record.get("account_id")
        else root.get("accounts") if isinstance(root.get("accounts"), list)
        else []
    )
    accounts: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        account_id = item.get("account_id", item.get("accountId", item.get("id")))
        if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id):
            continue
        accounts.append({
            "account_id": account_id,
            "account_type": (item.get("account_type", item.get("accountType")) if isinstance(item.get("account_type", item.get("accountType")), str) else None),
            "currency": item.get("currency") if isinstance(item.get("currency"), str) else None,
        })
    if not accounts:
        raise HTTPException(status_code=502, detail="Deriv returned no supported accounts.")

    await _creds._store_credential(body.user_id, access_token, expires_in)
    # Write account ownership links so _linked_account_ids() can verify them.
    account_ids = [a["account_id"] for a in accounts]
    await _creds._store_account_links(body.user_id, account_ids)
    return {"accounts": accounts, "expires_in": expires_in}


@router.post("/internal/credentials", include_in_schema=False, status_code=204)
async def transfer_credentials(
    body: CredentialTransfer,
    x_tradebridge_bridge_token: Annotated[str | None, Header()] = None,
) -> None:
    """Trusted Next.js OAuth callback transfers a token; token is never returned.

    Note: this endpoint transfers a credential only — it does not have access to
    the account list, so it cannot populate deriv_account_links. Account links are
    written by POST /internal/oauth/exchange which has the full account payload.
    """
    _require_bridge_secret(x_tradebridge_bridge_token)
    await _creds._store_credential(body.user_id, body.access_token.get_secret_value(), body.expires_in)


@router.delete("/internal/credentials/{user_id}", include_in_schema=False, status_code=204)
async def revoke_transferred_credentials(
    user_id: str,
    x_tradebridge_bridge_token: Annotated[str | None, Header()] = None,
) -> None:
    _require_bridge_secret(x_tradebridge_bridge_token)
    await _creds._delete_credential(user_id)
    await _creds._delete_account_links(user_id)


@router.get("/session")
async def session_status(user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    """Return safe connection metadata, never credentials or provider WebSocket URLs."""
    credential = await _creds._load_credential(user_id)
    if not credential or credential.expires_at <= time.time():
        if credential:
            await _creds._delete_credential(user_id)
        return {"connected": False}
    return {
        "connected": True,
        "credential_updated_at": int(credential.updated_at),
        "expires_at": int(credential.expires_at),
    }


@router.post("/internal/account-ownership", include_in_schema=False)
async def verify_account_ownership(
    body: AccountOwnershipRequest,
    x_tradebridge_bridge_token: Annotated[str | None, Header()] = None,
) -> dict[str, list[str]]:
    """Verify account ownership for trusted Next.js server routes only."""
    _require_bridge_secret(x_tradebridge_bridge_token)
    owned_ids = await _linked_account_ids(body.user_id, body.account_ids)
    return {"owned_account_ids": sorted(owned_ids)}


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
            await _creds._delete_credential(user_id)
            raise HTTPException(status_code=401, detail="Deriv authorization expired; reconnect.")
        if not response.is_success:
            raise HTTPException(status_code=502, detail="Deriv account lookup failed.")
        payload = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Deriv account lookup failed.")

    root = payload if isinstance(payload, dict) else {}
    data = root.get("data")
    data_record = data if isinstance(data, dict) else {}
    candidates = (
        data if isinstance(data, list)
        else data_record.get("accounts") if isinstance(data_record.get("accounts"), list)
        else [data_record] if data_record.get("account_id")
        else root.get("accounts") if isinstance(root.get("accounts"), list)
        else []
    )
    normalized: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        account_id = item.get("account_id", item.get("accountId", item.get("id")))
        if not isinstance(account_id, str) or not ACCOUNT_ID_RE.fullmatch(account_id):
            continue
        raw_balance = item.get("balance")
        try:
            balance = float(raw_balance) if raw_balance is not None and str(raw_balance).strip() else None
        except (TypeError, ValueError):
            balance = None
        if balance is not None and not math.isfinite(balance):
            balance = None
        normalized.append({
            "account_id": account_id,
            "balance": balance,
            "currency": item.get("currency") if isinstance(item.get("currency"), str) else None,
            "account_type": item.get("account_type", item.get("accountType")),
            "status": item.get("status") if isinstance(item.get("status"), str) else None,
        })
    linked_ids = await _linked_account_ids(user_id, [item["account_id"] for item in normalized])
    owned_accounts = [item for item in normalized if item["account_id"] in linked_ids]
    if not owned_accounts:
        raise HTTPException(status_code=409, detail="No Deriv accounts are linked to this app user.")
    return {"connected": True, "accounts": owned_accounts}


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
                await _creds._delete_credential(user_id)
                raise HTTPException(status_code=401, detail="Deriv authorization expired; reconnect.")
            if not accounts_response.is_success:
                raise HTTPException(status_code=502, detail="Deriv account verification failed.")
            accounts_payload = accounts_response.json()
            root = accounts_payload if isinstance(accounts_payload, dict) else {}
            data = root.get("data")
            accounts = (
                data if isinstance(data, list)
                else data.get("accounts", []) if isinstance(data, dict) and isinstance(data.get("accounts", []), list)
                else [data] if isinstance(data, dict) and data.get("account_id")
                else root.get("accounts", []) if isinstance(root.get("accounts"), list)
                else []
            )
            account = next((item for item in accounts if isinstance(item, dict) and item.get("account_id") == body.account_id), None)
            if account is None:
                raise HTTPException(status_code=403, detail="Account is not available to this Deriv authorization.")
            linked_ids = await _linked_account_ids(user_id, [body.account_id])
            if body.account_id not in linked_ids:
                raise HTTPException(status_code=403, detail="Account is not linked to this TradeBridge user.")
            account_type = str(account.get("account_type", "")).lower()
            if account_type not in ("demo", "real"):
                raise HTTPException(
                    status_code=403,
                    detail="Unsupported account type.",
                )
            otp_response = await client.post(
                f"https://api.derivws.com/trading/v1/options/accounts/{body.account_id}/otp",
                headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
            )
            if not otp_response.is_success:
                raise HTTPException(status_code=502, detail=f"Could not activate the {account_type} trading session.")
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
    expected_prefix = f"wss://api.derivws.com/trading/v1/options/ws/{account_type}?"
    if not isinstance(provider_url, str) or not provider_url.startswith(expected_prefix):
        raise HTTPException(status_code=502, detail=f"Deriv returned an invalid {account_type} session.")
    return {
        "active": False,
        "account_id": body.account_id,
        "account_type": account_type,
        "status": "provider_session_verified",
        "next_step": "server_websocket_proxy_not_yet_enabled",
    }


@router.delete("/session", status_code=204)
async def disconnect_deriv(user_id: Annotated[str, Depends(current_user)]) -> None:
    await _creds._delete_credential(user_id)
    await _creds._delete_account_links(user_id)
