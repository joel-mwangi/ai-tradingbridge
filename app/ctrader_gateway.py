"""Server-owned cTrader Open API OAuth, account discovery and demo order gateway.

All cTrader tokens stay encrypted in server-only storage. Order execution is fail-closed:
demo accounts only, explicit confirmation, server-side volume checks and a persistent
idempotency record are required before a market order can be sent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import httpx
import websockets
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictBool, StrictStr, field_validator
from starlette.concurrency import run_in_threadpool

from app.deriv_gateway import (
    _credential_cipher,
    _credential_store_config,
    _require_bridge_secret,
    current_user,
)

router = APIRouter(prefix="/ctrader", tags=["ctrader"])
ACCOUNT_ID_RE = re.compile(r"^[0-9]{1,24}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,50}$")
CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SGD",
    "HKD", "NOK", "SEK", "DKK", "ZAR", "MXN", "PLN", "TRY", "CNH",
    "CNY", "HUF", "CZK", "ILS", "THB",
}
DEMO_HOST = "demo1.p.ctrader.com"
LIVE_HOST = "live1.p.ctrader.com"
OPENAPI_PORT = 5035
TOKEN_ENDPOINT = "https://openapi.ctrader.com/apps/token"
AUTH_ENDPOINT = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"

# cTrader Open API payload IDs from Spotware's OpenApiModelMessages.proto.
APP_AUTH_REQ = 2100
APP_AUTH_RES = 2101
ACCOUNT_AUTH_REQ = 2102
ACCOUNT_AUTH_RES = 2103
NEW_ORDER_REQ = 2106
SYMBOLS_LIST_REQ = 2114
SYMBOLS_LIST_RES = 2115
SYMBOL_BY_ID_REQ = 2116
SYMBOL_BY_ID_RES = 2117
TRADER_REQ = 2121
TRADER_RES = 2122
RECONCILE_REQ = 2124
RECONCILE_RES = 2125
EXECUTION_EVENT = 2126
ORDER_ERROR_EVENT = 2132
ERROR_RES = 2142
ACCOUNT_LIST_REQ = 2149
ACCOUNT_LIST_RES = 2150


@dataclass(frozen=True)
class CTraderCredential:
    access_token: str
    refresh_token: str
    expires_at: float
    permission_scope: int
    updated_at: float


class CTraderOAuthExchange(BaseModel):
    user_id: StrictStr = Field(min_length=1, max_length=80)
    code: StrictStr = Field(min_length=1, max_length=512)
    redirect_uri: StrictStr = Field(min_length=1, max_length=2048)


class CTraderOrderRequest(BaseModel):
    account_id: StrictStr = Field(min_length=1, max_length=24)
    symbol_id: int = Field(gt=0)
    side: Literal["BUY", "SELL"]
    volume_units: float = Field(gt=0, le=100_000)
    idempotency_key: StrictStr = Field(min_length=8, max_length=50)
    confirmed: StrictBool

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, value: str) -> str:
        if not ACCOUNT_ID_RE.fullmatch(value):
            raise ValueError("Invalid cTrader account identifier.")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not IDEMPOTENCY_RE.fullmatch(value):
            raise ValueError("Invalid idempotency key.")
        return value

    @field_validator("volume_units")
    @classmethod
    def validate_volume_is_finite(cls, value: float) -> float:
        if not (value > 0 and value < float("inf")):
            raise ValueError("Volume must be a finite positive number.")
        return value


class CTraderProviderError(Exception):
    def __init__(self, error_code: str, description: str = "") -> None:
        self.error_code = error_code[:100]
        self.description = description[:400]
        super().__init__(self.error_code)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(status_code=503, detail=f"{name} is not configured.")
    return value


def _ctrader_app_credentials() -> tuple[str, str]:
    return (
        _required_env("CTRADER_OPENAPI_CLIENT_ID"),
        _required_env("CTRADER_OPENAPI_CLIENT_SECRET"),
    )


def _store_config() -> tuple[str, str]:
    return _credential_store_config()


def _encrypt(value: str) -> str:
    return _credential_cipher().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=503, detail="Stored cTrader credentials are invalid.")
    try:
        return _credential_cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeEncodeError):
        raise HTTPException(status_code=503, detail="Stored cTrader credentials cannot be decrypted.")


async def _store_credential(
    user_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    permission_scope: int,
) -> CTraderCredential:
    url, service_key = _store_config()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(60, min(expires_in, 2_700_000)))
    row = {
        "user_id": user_id,
        "encrypted_access_token": _encrypt(access_token),
        "encrypted_refresh_token": _encrypt(refresh_token),
        "expires_at": expires_at.isoformat(),
        "permission_scope": permission_scope,
        "updated_at": now.isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{url}/rest/v1/ctrader_backend_credentials",
                params={"on_conflict": "user_id"},
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                json=row,
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not securely store the cTrader credential.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not securely store the cTrader credential.")
    return CTraderCredential(access_token, refresh_token, expires_at.timestamp(), permission_scope, now.timestamp())


async def _load_credential(user_id: str) -> CTraderCredential | None:
    url, service_key = _store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                f"{url}/rest/v1/ctrader_backend_credentials",
                params={
                    "select": "encrypted_access_token,encrypted_refresh_token,expires_at,permission_scope,updated_at",
                    "user_id": f"eq.{user_id}",
                    "limit": "1",
                },
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not load the cTrader credential.")
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(status_code=503, detail="cTrader credential storage returned invalid data.")
        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict):
            raise HTTPException(status_code=503, detail="cTrader credential storage returned invalid data.")
        expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")).timestamp()
        updated_at = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00")).timestamp()
        permission_scope = row.get("permission_scope")
        if type(permission_scope) is not int:
            raise HTTPException(status_code=503, detail="Stored cTrader permissions are invalid.")
        return CTraderCredential(
            _decrypt(row.get("encrypted_access_token")),
            _decrypt(row.get("encrypted_refresh_token")),
            expires_at,
            permission_scope,
            updated_at,
        )
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
        raise HTTPException(status_code=503, detail="Could not load the cTrader credential.")


async def _delete_credential(user_id: str) -> None:
    url, service_key = _store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.delete(
                f"{url}/rest/v1/ctrader_backend_credentials",
                params={"user_id": f"eq.{user_id}"},
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not revoke the cTrader credential.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not revoke the cTrader credential.")


async def _store_account_links(user_id: str, accounts: list[dict[str, Any]]) -> None:
    if not accounts:
        raise HTTPException(status_code=502, detail="cTrader returned no authorized accounts.")
    url, service_key = _store_config()
    rows = [{
        "user_id": user_id,
        "ctid_trader_account_id": account["account_id"],
        "is_live": account["is_live"],
        "trader_login": account.get("trader_login"),
        "broker_title_short": account.get("broker_title_short"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    } for account in accounts]
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{url}/rest/v1/ctrader_account_links",
                params={"on_conflict": "ctid_trader_account_id"},
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=ignore-duplicates,return=minimal",
                },
                json=rows,
            )
        if not response.is_success:
            if response.status_code == 409:
                raise HTTPException(status_code=409, detail="A cTrader account is already linked to another TradeBridge user.")
            raise HTTPException(status_code=503, detail="Could not securely store cTrader account ownership.")
        linked = await _load_account_links(user_id)
        linked_ids = {item["account_id"] for item in linked}
        if any(account["account_id"] not in linked_ids for account in accounts):
            raise HTTPException(status_code=409, detail="One or more cTrader accounts are linked to another TradeBridge user.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not securely store cTrader account ownership.")


async def _load_account_links(user_id: str) -> list[dict[str, Any]]:
    url, service_key = _store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                f"{url}/rest/v1/ctrader_account_links",
                params={
                    "select": "ctid_trader_account_id,is_live,trader_login,broker_title_short",
                    "user_id": f"eq.{user_id}",
                    "order": "is_live.asc,ctid_trader_account_id.asc",
                },
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not load cTrader account ownership.")
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(status_code=503, detail="cTrader account storage returned invalid data.")
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            account_id = row.get("ctid_trader_account_id")
            if isinstance(account_id, (str, int)) and ACCOUNT_ID_RE.fullmatch(str(account_id)):
                result.append({
                    "account_id": str(account_id),
                    "account_type": "live" if row.get("is_live") is True else "demo",
                    "trader_login": row.get("trader_login") if isinstance(row.get("trader_login"), int) else None,
                    "broker_title_short": row.get("broker_title_short") if isinstance(row.get("broker_title_short"), str) else None,
                })
        return result
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(status_code=503, detail="Could not load cTrader account ownership.")


async def _delete_account_links(user_id: str) -> None:
    url, service_key = _store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.delete(
                f"{url}/rest/v1/ctrader_account_links",
                params={"user_id": f"eq.{user_id}"},
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not remove cTrader account links.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not remove cTrader account links.")


async def _update_order_request(
    user_id: str,
    idempotency_key: str,
    status_value: str,
    order_id: int | None = None,
    position_id: int | None = None,
    provider_error: str | None = None,
) -> None:
    url, service_key = _store_config()
    payload: dict[str, Any] = {
        "status": status_value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if order_id is not None:
        payload["provider_order_id"] = str(order_id)
    if position_id is not None:
        payload["provider_position_id"] = str(position_id)
    if provider_error:
        payload["provider_error_code"] = provider_error[:100]
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.patch(
                f"{url}/rest/v1/ctrader_order_requests",
                params={"user_id": f"eq.{user_id}", "idempotency_key": f"eq.{idempotency_key}"},
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=payload,
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not persist cTrader order status.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not persist cTrader order status.")


async def _claim_order(user_id: str, body: CTraderOrderRequest, volume_cents: int) -> None:
    url, service_key = _store_config()
    payload_hash = hashlib.sha256(
        f"{body.account_id}|{body.symbol_id}|{body.side}|{volume_cents}".encode("utf-8")
    ).hexdigest()
    row = {
        "user_id": user_id,
        "idempotency_key": body.idempotency_key,
        "ctid_trader_account_id": body.account_id,
        "symbol_id": body.symbol_id,
        "side": body.side,
        "volume_cents": volume_cents,
        "request_hash": payload_hash,
        "status": "submitting",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{url}/rest/v1/ctrader_order_requests",
                params={"on_conflict": "user_id,idempotency_key"},
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=ignore-duplicates,return=representation",
                },
                json=row,
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not create a safe cTrader order record.")
        inserted = response.json()
        if not isinstance(inserted, list) or not inserted:
            raise HTTPException(
                status_code=409,
                detail="This order request was already submitted or has an unresolved outcome. Do not retry it; check order status first.",
            )
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Could not create a safe cTrader order record.")


async def _refresh_credential(credential: CTraderCredential) -> CTraderCredential:
    client_id, client_secret = _ctrader_app_credentials()
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                TOKEN_ENDPOINT,
                params={
                    "grant_type": "refresh_token",
                    "refresh_token": credential.refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
            )
        if not response.is_success:
            raise HTTPException(status_code=401, detail="cTrader authorization expired; reconnect your account.")
        payload = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Could not refresh the cTrader authorization.")
    access_token = payload.get("accessToken") if isinstance(payload, dict) else None
    refresh_token = payload.get("refreshToken") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token or not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(status_code=401, detail="cTrader authorization cannot be refreshed; reconnect your account.")
    try:
        expires_in = int(payload.get("expiresIn", 2_628_000))
    except (TypeError, ValueError):
        expires_in = 2_628_000
    user_id = getattr(_refresh_credential, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=503, detail="Could not resolve cTrader credential owner.")
    return await _store_credential(user_id, access_token, refresh_token, expires_in, credential.permission_scope)


async def _owned_credential(user_id: str) -> CTraderCredential:
    credential = await _load_credential(user_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="cTrader is not connected to this TradeBridge account.")
    if credential.expires_at <= time.time() + 120:
        # Refresh tokens are user-specific; the attribute is set only for this awaited refresh.
        setattr(_refresh_credential, "user_id", user_id)
        return await _refresh_credential(credential)
    return credential


async def _receive_for(
    websocket: Any,
    client_msg_id: str,
    expected_types: set[int],
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cTrader did not return a response in time.")
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        response_id = message.get("clientMsgId")
        response_type = message.get("payloadType")
        if response_id != client_msg_id:
            continue
        try:
            response_type = int(response_type)
        except (TypeError, ValueError):
            raise CTraderProviderError("INVALID_RESPONSE", "cTrader returned an invalid message type.")
        payload = message.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if response_type == ERROR_RES:
            raise CTraderProviderError(str(payload.get("errorCode", "PROVIDER_ERROR")), str(payload.get("description", "")))
        if response_type not in expected_types:
            raise CTraderProviderError("UNEXPECTED_RESPONSE", "cTrader returned an unexpected response.")
        return payload


async def _send_request(
    websocket: Any,
    payload_type: int,
    payload: dict[str, Any],
    expected_types: set[int],
    timeout_seconds: float = 10.0,
) -> tuple[dict[str, Any], int]:
    msg_id = uuid.uuid4().hex
    await websocket.send(json.dumps({
        "clientMsgId": msg_id,
        "payloadType": payload_type,
        "payload": payload,
    }, separators=(",", ":")))
    response_type = await _receive_for(websocket, msg_id, expected_types, timeout_seconds)
    return response_type, {}


async def _send_and_wait(
    websocket: Any,
    payload_type: int,
    payload: dict[str, Any],
    expected_types: set[int],
    timeout_seconds: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    msg_id = uuid.uuid4().hex
    await websocket.send(json.dumps({
        "clientMsgId": msg_id,
        "payloadType": payload_type,
        "payload": payload,
    }, separators=(",", ":")))
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cTrader did not return a response in time.")
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict) or message.get("clientMsgId") != msg_id:
            continue
        try:
            response_type = int(message.get("payloadType"))
        except (TypeError, ValueError):
            raise CTraderProviderError("INVALID_RESPONSE", "cTrader returned an invalid message type.")
        response_payload = message.get("payload")
        response_payload = response_payload if isinstance(response_payload, dict) else {}
        if response_type == ERROR_RES:
            raise CTraderProviderError(str(response_payload.get("errorCode", "PROVIDER_ERROR")), str(response_payload.get("description", "")))
        if response_type not in expected_types:
            raise CTraderProviderError("UNEXPECTED_RESPONSE", "cTrader returned an unexpected response.")
        return response_type, response_payload


async def _connect_and_authorize_application(websocket: Any) -> None:
    client_id, client_secret = _ctrader_app_credentials()
    await _send_and_wait(
        websocket, APP_AUTH_REQ, {"clientId": client_id, "clientSecret": client_secret}, {APP_AUTH_RES}
    )


async def _list_accounts_on_host(host: str, access_token: str) -> list[dict[str, Any]]:
    async with websockets.connect(
        f"wss://{host}:{OPENAPI_PORT}",
        open_timeout=8,
        close_timeout=3,
        ping_interval=20,
        max_size=1_048_576,
    ) as websocket:
        await _connect_and_authorize_application(websocket)
        _, payload = await _send_and_wait(
            websocket, ACCOUNT_LIST_REQ, {"accessToken": access_token}, {ACCOUNT_LIST_RES}
        )
    candidates = payload.get("ctidTraderAccount", [])
    if not isinstance(candidates, list):
        return []
    accounts: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        account_id = item.get("ctidTraderAccountId")
        if not isinstance(account_id, (str, int)) or not ACCOUNT_ID_RE.fullmatch(str(account_id)):
            continue
        accounts.append({
            "account_id": str(account_id),
            "is_live": item.get("isLive") is True,
            "trader_login": item.get("traderLogin") if isinstance(item.get("traderLogin"), int) else None,
            "broker_title_short": item.get("brokerTitleShort") if isinstance(item.get("brokerTitleShort"), str) else None,
        })
    return accounts


async def _list_authorized_accounts(access_token: str) -> list[dict[str, Any]]:
    accounts: dict[str, dict[str, Any]] = {}
    failures: list[Exception] = []
    for host in (DEMO_HOST, LIVE_HOST):
        try:
            for account in await _list_accounts_on_host(host, access_token):
                accounts[account["account_id"]] = account
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException, CTraderProviderError) as exc:
            failures.append(exc)
    if not accounts and failures:
        raise HTTPException(status_code=502, detail="Could not verify cTrader accounts through Open API.")
    return list(accounts.values())


async def _call_account_api(
    credential: CTraderCredential,
    account_id: str,
    is_live: bool,
    payload_type: int,
    payload: dict[str, Any],
    expected_types: set[int],
    timeout_seconds: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    host = LIVE_HOST if is_live else DEMO_HOST
    async with websockets.connect(
        f"wss://{host}:{OPENAPI_PORT}",
        open_timeout=8,
        close_timeout=3,
        ping_interval=20,
        max_size=1_048_576,
    ) as websocket:
        await _connect_and_authorize_application(websocket)
        await _send_and_wait(
            websocket,
            ACCOUNT_AUTH_REQ,
            {"ctidTraderAccountId": int(account_id), "accessToken": credential.access_token},
            {ACCOUNT_AUTH_RES},
        )
        return await _send_and_wait(
            websocket, payload_type, payload, expected_types, timeout_seconds=timeout_seconds
        )


def _is_forex_symbol(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"[^A-Z]", "", value.upper())
    return len(normalized) == 6 and normalized[:3] in CURRENCY_CODES and normalized[3:] in CURRENCY_CODES


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": account["account_id"],
        "account_type": account["account_type"],
        "trader_login": account.get("trader_login"),
        "broker_title_short": account.get("broker_title_short"),
    }


@router.post("/internal/oauth/exchange", include_in_schema=False)
async def exchange_oauth_code(
    body: CTraderOAuthExchange,
    x_tradebridge_bridge_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_bridge_secret(x_tradebridge_bridge_token)
    user_id = body.user_id
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", user_id):
        raise HTTPException(status_code=400, detail="Invalid TradeBridge user identifier.")
    client_id, client_secret = _ctrader_app_credentials()
    configured_redirect = os.getenv("CTRADER_OAUTH_REDIRECT_URI", "").strip()
    if not configured_redirect:
        app_url = os.getenv("APP_URL", "").strip().rstrip("/")
        configured_redirect = f"{app_url}/api/auth/ctrader/callback" if app_url else ""
    if not configured_redirect or not hmac.compare_digest(body.redirect_uri, configured_redirect):
        raise HTTPException(status_code=400, detail="cTrader OAuth redirect URI does not match backend configuration.")

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(
                TOKEN_ENDPOINT,
                params={
                    "grant_type": "authorization_code",
                    "code": body.code,
                    "redirect_uri": configured_redirect,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
            )
        if not response.is_success:
            raise HTTPException(status_code=502, detail="cTrader authorization-code exchange failed.")
        token_payload = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Could not exchange the cTrader authorization code.")

    if not isinstance(token_payload, dict) or token_payload.get("errorCode"):
        raise HTTPException(status_code=502, detail="cTrader rejected the authorization-code exchange.")
    access_token = token_payload.get("accessToken")
    refresh_token = token_payload.get("refreshToken")
    if not isinstance(access_token, str) or not access_token or not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(status_code=502, detail="cTrader returned an incomplete token response.")
    try:
        expires_in = max(60, min(int(token_payload.get("expiresIn", 2_628_000)), 2_700_000))
    except (TypeError, ValueError):
        expires_in = 2_628_000

    accounts = await _list_authorized_accounts(access_token)
    if not accounts:
        raise HTTPException(status_code=502, detail="No cTrader accounts were authorized. Reconnect and choose an account.")
    permission_scope = 1 if token_payload.get("permissionScope") in (1, "SCOPE_TRADE", "trading", None) else 0
    # Store ownership links only after Open API verifies that the access token can enumerate them.
    await _store_account_links(user_id, accounts)
    try:
        await _store_credential(user_id, access_token, refresh_token, expires_in, permission_scope)
    except HTTPException:
        await _delete_account_links(user_id)
        raise
    return {
        "connected": True,
        "accounts": [
            _public_account({**account, "account_type": "live" if account["is_live"] else "demo"})
            for account in accounts
        ],
        "permission_scope": "trading" if permission_scope == 1 else "accounts",
        "expires_in": expires_in,
    }


@router.get("/session")
async def session_status(user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    credential = await _load_credential(user_id)
    links = await _load_account_links(user_id)
    return {
        "connected": credential is not None and bool(links),
        "accounts": [_public_account(item) for item in links],
        "permission_scope": "trading" if credential and credential.permission_scope == 1 else "accounts",
        "expires_at": int(credential.expires_at) if credential else None,
    }


@router.get("/accounts")
async def accounts(user_id: Annotated[str, Depends(current_user)]) -> dict[str, Any]:
    credential = await _owned_credential(user_id)
    links = await _load_account_links(user_id)
    if not links:
        raise HTTPException(status_code=409, detail="No verified cTrader account is linked.")
    return {
        "connected": True,
        "accounts": [_public_account(item) for item in links],
        "permission_scope": "trading" if credential.permission_scope == 1 else "accounts",
    }


@router.delete("/session", status_code=204)
async def disconnect(user_id: Annotated[str, Depends(current_user)]) -> None:
    await _delete_credential(user_id)
    await _delete_account_links(user_id)


@router.get("/symbols")
async def symbols(
    user_id: Annotated[str, Depends(current_user)],
    account_id: str = Query(min_length=1, max_length=24),
) -> dict[str, Any]:
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        raise HTTPException(status_code=422, detail="Invalid cTrader account identifier.")
    credential = await _owned_credential(user_id)
    links = await _load_account_links(user_id)
    account = next((item for item in links if item["account_id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=403, detail="This cTrader account is not linked to your TradeBridge user.")
    try:
        _, payload = await _call_account_api(
            credential,
            account_id,
            account["account_type"] == "live",
            SYMBOLS_LIST_REQ,
            {"ctidTraderAccountId": int(account_id), "includeArchivedSymbols": False},
            {SYMBOLS_LIST_RES},
        )
    except CTraderProviderError:
        raise HTTPException(status_code=502, detail="cTrader could not return symbols for this account.")
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException):
        raise HTTPException(status_code=502, detail="Could not connect to cTrader Open API.")
    candidates = payload.get("symbol", [])
    output: list[dict[str, Any]] = []
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            name = item.get("symbolName")
            symbol_id = item.get("symbolId")
            if _is_forex_symbol(name) and isinstance(symbol_id, (int, str)) and str(symbol_id).isdigit():
                output.append({"symbol_id": int(symbol_id), "symbol_name": name})
    output.sort(key=lambda row: row["symbol_name"])
    return {"account_id": account_id, "symbols": output}


@router.post("/orders")
async def place_demo_order(
    body: CTraderOrderRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    if body.confirmed is not True:
        raise HTTPException(status_code=400, detail="Explicit order confirmation is required.")
    if os.getenv("CTRADER_DEMO_ORDERS_ENABLED", "").strip().lower() != "true":
        raise HTTPException(status_code=503, detail="Demo order execution is disabled until configured by the TradeBridge operator.")
    max_volume = 100.0
    try:
        max_volume = min(100_000.0, max(0.01, float(os.getenv("CTRADER_DEMO_MAX_VOLUME_UNITS", "100"))))
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="The server-side cTrader demo volume limit is invalid.")
    if body.volume_units > max_volume:
        raise HTTPException(status_code=422, detail=f"Demo order volume exceeds the server limit of {max_volume:g} units.")

    credential = await _owned_credential(user_id)
    if credential.permission_scope != 1:
        raise HTTPException(status_code=403, detail="Trading permission was not granted. Reconnect and approve trading access.")
    links = await _load_account_links(user_id)
    account = next((item for item in links if item["account_id"] == body.account_id), None)
    if not account:
        raise HTTPException(status_code=403, detail="This cTrader account is not linked to your TradeBridge user.")
    if account["account_type"] != "demo":
        raise HTTPException(status_code=403, detail="Real-account order execution is locked. Select a cTrader Demo account.")

    # Resolve the requested symbol against cTrader's own account-specific catalogue.
    try:
        _, light_payload = await _call_account_api(
            credential,
            body.account_id,
            False,
            SYMBOLS_LIST_REQ,
            {"ctidTraderAccountId": int(body.account_id), "includeArchivedSymbols": False},
            {SYMBOLS_LIST_RES},
        )
        light_symbols = light_payload.get("symbol", [])
        allowed_symbol = next(
            (
                item for item in light_symbols
                if isinstance(item, dict)
                and int(item.get("symbolId", -1)) == body.symbol_id
                and item.get("enabled") is not False
                and _is_forex_symbol(item.get("symbolName"))
            ),
            None,
        ) if isinstance(light_symbols, list) else None
        if not allowed_symbol:
            raise HTTPException(status_code=422, detail="Select an enabled Forex symbol from the linked cTrader account.")
        _, full_payload = await _call_account_api(
            credential,
            body.account_id,
            False,
            SYMBOL_BY_ID_REQ,
            {"ctidTraderAccountId": int(body.account_id), "symbolId": [body.symbol_id]},
            {SYMBOL_BY_ID_RES},
        )
    except HTTPException:
        raise
    except CTraderProviderError:
        raise HTTPException(status_code=502, detail="cTrader could not validate the selected Forex symbol.")
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException):
        raise HTTPException(status_code=502, detail="Could not connect to cTrader to validate the order.")

    full_symbols = full_payload.get("symbol", [])
    full_symbol = next(
        (item for item in full_symbols if isinstance(item, dict) and int(item.get("symbolId", -1)) == body.symbol_id),
        None,
    ) if isinstance(full_symbols, list) else None
    if not full_symbol:
        raise HTTPException(status_code=502, detail="cTrader did not return the symbol's trading constraints.")
    volume_cents = int(round(body.volume_units * 100))
    min_volume = int(full_symbol.get("minVolume", 0) or 0)
    max_symbol_volume = int(full_symbol.get("maxVolume", 0) or 0)
    step_volume = int(full_symbol.get("stepVolume", 0) or 0)
    if min_volume <= 0 or max_symbol_volume < min_volume or step_volume <= 0:
        raise HTTPException(status_code=422, detail="cTrader did not provide valid volume rules for this symbol.")
    if volume_cents < min_volume or volume_cents > max_symbol_volume or (volume_cents - min_volume) % step_volume != 0:
        raise HTTPException(
            status_code=422,
            detail=f"Volume must comply with this symbol's cTrader size rules (min {min_volume / 100:g}, max {max_symbol_volume / 100:g}, step {step_volume / 100:g} units).",
        )

    await _claim_order(user_id, body, volume_cents)
    client_order_id = body.idempotency_key[:50]
    request_payload = {
        "ctidTraderAccountId": int(body.account_id),
        "symbolId": body.symbol_id,
        "orderType": 1,
        "tradeSide": 1 if body.side == "BUY" else 2,
        "volume": volume_cents,
        "clientOrderId": client_order_id,
        "comment": "TradeBridge user-confirmed demo order",
    }
    try:
        _, payload = await _call_account_api(
            credential,
            body.account_id,
            False,
            NEW_ORDER_REQ,
            request_payload,
            {EXECUTION_EVENT, ORDER_ERROR_EVENT},
            timeout_seconds=12.0,
        )
    except CTraderProviderError as exc:
        await _update_order_request(user_id, body.idempotency_key, "rejected", provider_error=exc.error_code)
        raise HTTPException(status_code=422, detail="cTrader rejected the demo order: " + exc.error_code)
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException):
        await _update_order_request(user_id, body.idempotency_key, "unknown")
        raise HTTPException(
            status_code=504,
            detail="The order outcome is unknown because cTrader did not confirm it. Do not submit it again; check the cTrader account before taking further action.",
        )

    if payload.get("errorCode") or payload.get("executionType") == 7:
        error_code = str(payload.get("errorCode") or "ORDER_REJECTED")
        await _update_order_request(user_id, body.idempotency_key, "rejected", provider_error=error_code)
        raise HTTPException(status_code=422, detail="cTrader rejected the demo order: " + error_code)

    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
    order_id = order.get("orderId") if isinstance(order.get("orderId"), int) else None
    position_id = position.get("positionId") if isinstance(position.get("positionId"), int) else None
    if payload.get("executionType") not in (2, 3, 11) and order_id is None and position_id is None:
        await _update_order_request(user_id, body.idempotency_key, "unknown")
        raise HTTPException(
            status_code=504,
            detail="cTrader returned an unrecognized execution state. The outcome is unknown; do not retry this order.",
        )
    await _update_order_request(user_id, body.idempotency_key, "accepted", order_id, position_id)
    return {
        "status": "accepted",
        "account_id": body.account_id,
        "symbol_id": body.symbol_id,
        "side": body.side,
        "volume_units": body.volume_units,
        "order_id": str(order_id) if order_id is not None else None,
        "position_id": str(position_id) if position_id is not None else None,
        "message": "cTrader acknowledged the demo order. Verify its final fill and position in account activity.",
    }


@router.get("/orders/{idempotency_key}")
async def order_status(
    idempotency_key: str,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    if not IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise HTTPException(status_code=422, detail="Invalid order reference.")
    url, service_key = _store_config()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                f"{url}/rest/v1/ctrader_order_requests",
                params={
                    "select": "idempotency_key,ctid_trader_account_id,symbol_id,side,volume_cents,status,provider_order_id,provider_position_id,provider_error_code,created_at,updated_at",
                    "user_id": f"eq.{user_id}",
                    "idempotency_key": f"eq.{idempotency_key}",
                    "limit": "1",
                },
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Could not load cTrader order status.")
        rows = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Could not load cTrader order status.")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=404, detail="No order request exists with this reference.")
    row = rows[0]
    if not isinstance(row, dict):
        raise HTTPException(status_code=502, detail="cTrader order status is malformed.")
    return {
        "idempotency_key": row.get("idempotency_key"),
        "account_id": str(row.get("ctid_trader_account_id", "")),
        "symbol_id": row.get("symbol_id"),
        "side": row.get("side"),
        "volume_units": (row.get("volume_cents", 0) / 100),
        "status": row.get("status"),
        "order_id": row.get("provider_order_id"),
        "position_id": row.get("provider_position_id"),
        "error_code": row.get("provider_error_code"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
