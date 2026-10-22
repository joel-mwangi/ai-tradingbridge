"""Server-owned cTrader Open API OAuth, account discovery and demo order gateway.

All cTrader tokens stay encrypted in server-only storage. Order execution is fail-closed:
demo accounts only, explicit confirmation, server-side volume checks and a persistent
idempotency record are required before a market order can be sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import httpx
import websockets
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool, StrictStr, field_validator

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
DEMO_HOST = "demo.ctraderapi.com"
LIVE_HOST = "live.ctraderapi.com"
OPENAPI_PORT = 5036
TOKEN_ENDPOINT = "https://openapi.ctrader.com/apps/token"

# cTrader Open API payload IDs from Spotware's OpenApiModelMessages.proto.
APP_AUTH_REQ = 2100
APP_AUTH_RES = 2101
ACCOUNT_AUTH_REQ = 2102
ACCOUNT_AUTH_RES = 2103
NEW_ORDER_REQ = 2106
SPOTS_SUBSCRIBE_REQ = 2127
SPOTS_SUBSCRIBE_RES = 2128
SPOT_EVENT = 2131
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
AMEND_POSITION_SLTP_REQ = 2110
CLOSE_POSITION_REQ = 2111
RECONCILE_REQ = 2124
RECONCILE_RES = 2125
GET_POSITION_UNREALIZED_PNL_REQ = 2187
GET_POSITION_UNREALIZED_PNL_RES = 2188
POSITION_ID_RE = re.compile(r"^[0-9]{1,24}$")


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


class CTraderProtectionRequest(BaseModel):
    account_id: StrictStr = Field(min_length=1, max_length=24)
    idempotency_key: StrictStr = Field(min_length=8, max_length=50)
    stop_loss: float | None = None
    take_profit: float | None = None
    confirmed: StrictBool

    @field_validator("account_id")
    @classmethod
    def validate_position_action_account_id(cls, value: str) -> str:
        if not ACCOUNT_ID_RE.fullmatch(value):
            raise ValueError("Invalid cTrader account identifier.")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_position_action_key(cls, value: str) -> str:
        if not IDEMPOTENCY_RE.fullmatch(value):
            raise ValueError("Invalid idempotency key.")
        return value

    @field_validator("stop_loss", "take_profit")
    @classmethod
    def validate_protection_price(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("Protection prices must be finite positive prices.")
        return value


class CTraderClosePositionRequest(BaseModel):
    account_id: StrictStr = Field(min_length=1, max_length=24)
    idempotency_key: StrictStr = Field(min_length=8, max_length=50)
    volume_units: float | None = Field(default=None, gt=0, le=100_000)
    confirmed: StrictBool

    @field_validator("account_id")
    @classmethod
    def validate_close_account_id(cls, value: str) -> str:
        if not ACCOUNT_ID_RE.fullmatch(value):
            raise ValueError("Invalid cTrader account identifier.")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_close_idempotency_key(cls, value: str) -> str:
        if not IDEMPOTENCY_RE.fullmatch(value):
            raise ValueError("Invalid idempotency key.")
        return value

    @field_validator("volume_units")
    @classmethod
    def validate_close_volume(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("Close volume must be a finite positive number.")
        if value is not None and abs(value * 100 - round(value * 100)) > 1e-7:
            raise ValueError("Close volume must use increments of 0.01 units.")
        return value


class CTraderProviderError(Exception):
    def __init__(self, error_code: str, description: str = "") -> None:
        self.error_code = error_code[:100]
        self.description = description[:400]
        super().__init__(self.error_code)


def _execution_type_value(payload: dict[str, Any]) -> int | None:
    raw = payload.get("executionType")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().upper()
        if normalized.isdigit():
            return int(normalized)
        return {
            "ORDER_ACCEPTED": 2,
            "ORDER_FILLED": 3,
            "ORDER_REJECTED": 7,
            "ORDER_PARTIAL_FILL": 11,
        }.get(normalized)
    return None


def _numeric_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _optional_protocol_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    return None


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


async def _claim_position_action(
    user_id: str,
    idempotency_key: str,
    account_id: str,
    position_id: int,
    action_type: str,
    request_hash: str,
) -> None:
    url, service_key = _store_config()
    row = {
        "user_id": user_id,
        "idempotency_key": idempotency_key,
        "ctid_trader_account_id": account_id,
        "position_id": position_id,
        "action_type": action_type,
        "request_hash": request_hash,
        "status": "submitting",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{url}/rest/v1/ctrader_position_action_requests",
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
            raise HTTPException(status_code=503, detail="Could not create a safe position-action record.")
        inserted = response.json()
        if not isinstance(inserted, list) or not inserted:
            raise HTTPException(
                status_code=409,
                detail="This position action was already submitted or its outcome is unresolved. Refresh positions before trying again.",
            )
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Could not create a safe position-action record.")


async def _update_position_action(
    user_id: str,
    idempotency_key: str,
    status_value: str,
    provider_error: str | None = None,
) -> None:
    url, service_key = _store_config()
    payload: dict[str, Any] = {
        "status": status_value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if provider_error:
        payload["provider_error_code"] = provider_error[:100]
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.patch(
                f"{url}/rest/v1/ctrader_position_action_requests",
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
            raise HTTPException(status_code=503, detail="Could not persist the position-action status.")
    except HTTPException:
        raise
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Could not persist the position-action status.")


async def _provider_reconcile(
    credential: CTraderCredential,
    account_id: str,
    is_live: bool,
) -> dict[str, Any]:
    _, payload = await _call_account_api(
        credential,
        account_id,
        is_live,
        RECONCILE_REQ,
        {"ctidTraderAccountId": int(account_id), "returnProtectionOrders": False},
        {RECONCILE_RES},
    )
    return payload


def _as_protocol_int(value: Any) -> int | None:
    parsed = _optional_protocol_id(value)
    return parsed if parsed is not None else None


def _open_position_by_id(payload: dict[str, Any], position_id: int) -> dict[str, Any] | None:
    raw_positions = payload.get("position", [])
    if not isinstance(raw_positions, list):
        return None
    for item in raw_positions:
        if not isinstance(item, dict) or _as_protocol_int(item.get("positionId")) != position_id:
            continue
        status = _as_protocol_int(item.get("positionStatus"))
        if status not in (None, 1):
            continue
        return item
    return None


async def _position_snapshot(
    user_id: str,
    account_id: str,
    account_type: str,
    credential: CTraderCredential,
) -> dict[str, Any]:
    is_live = account_type == "live"
    payload = await _provider_reconcile(credential, account_id, is_live)
    raw_positions = payload.get("position", [])
    raw_orders = payload.get("order", [])
    raw_positions = raw_positions if isinstance(raw_positions, list) else []
    raw_orders = raw_orders if isinstance(raw_orders, list) else []

    symbol_names: dict[int, str] = {}
    try:
        _, symbol_payload = await _call_account_api(
            credential,
            account_id,
            is_live,
            SYMBOLS_LIST_REQ,
            {"ctidTraderAccountId": int(account_id), "includeArchivedSymbols": False},
            {SYMBOLS_LIST_RES},
        )
        for symbol in symbol_payload.get("symbol", []):
            if not isinstance(symbol, dict):
                continue
            symbol_id = _as_protocol_int(symbol.get("symbolId"))
            name = symbol.get("symbolName")
            if symbol_id is not None and isinstance(name, str):
                symbol_names[symbol_id] = name
    except (CTraderProviderError, TimeoutError, OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
        # Reconcile data is still authoritative; labels can fall back to symbol IDs.
        pass

    pnl_by_position: dict[int, float] = {}
    pnl_money_digits: int | None = None
    if raw_positions:
        try:
            _, pnl_payload = await _call_account_api(
                credential,
                account_id,
                is_live,
                GET_POSITION_UNREALIZED_PNL_REQ,
                {"ctidTraderAccountId": int(account_id)},
                {GET_POSITION_UNREALIZED_PNL_RES},
            )
            raw_pnls = pnl_payload.get("positionUnrealizedPnL", [])
            pnl_money_digits = max(0, min(12, int(pnl_payload.get("moneyDigits", 2))))
            scale = 10 ** pnl_money_digits
            if isinstance(raw_pnls, list):
                for pnl in raw_pnls:
                    if not isinstance(pnl, dict):
                        continue
                    pnl_id = _as_protocol_int(pnl.get("positionId"))
                    raw_net = pnl.get("netUnrealizedPnL")
                    if pnl_id is None or isinstance(raw_net, bool):
                        continue
                    try:
                        net_value = float(raw_net)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if math.isfinite(net_value):
                        pnl_by_position[pnl_id] = net_value / scale
        except (CTraderProviderError, TimeoutError, OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError, ValueError, TypeError):
            pnl_by_position = {}
            pnl_money_digits = None

    positions: list[dict[str, Any]] = []
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        status = _as_protocol_int(item.get("positionStatus"))
        if status not in (None, 1):
            continue
        position_id = _as_protocol_int(item.get("positionId"))
        trade_data = item.get("tradeData") if isinstance(item.get("tradeData"), dict) else {}
        symbol_id = _as_protocol_int(trade_data.get("symbolId"))
        side_value = _as_protocol_int(trade_data.get("tradeSide"))
        volume_raw = _as_protocol_int(trade_data.get("volume"))
        entry_raw = item.get("price")
        try:
            entry_price = float(entry_raw) if entry_raw is not None else None
        except (TypeError, ValueError, OverflowError):
            entry_price = None
        if entry_price is not None and not math.isfinite(entry_price):
            entry_price = None
        stop_loss = item.get("stopLoss")
        take_profit = item.get("takeProfit")
        try:
            stop_loss = float(stop_loss) if stop_loss is not None else None
            take_profit = float(take_profit) if take_profit is not None else None
        except (TypeError, ValueError, OverflowError):
            stop_loss = take_profit = None
        if stop_loss is not None and (not math.isfinite(stop_loss) or stop_loss <= 0):
            stop_loss = None
        if take_profit is not None and (not math.isfinite(take_profit) or take_profit <= 0):
            take_profit = None
        if position_id is None or symbol_id is None or volume_raw is None:
            continue
        positions.append({
            "position_id": str(position_id),
            "symbol_id": symbol_id,
            "symbol_name": symbol_names.get(symbol_id, f"Symbol {symbol_id}"),
            "side": "BUY" if side_value == 1 else "SELL" if side_value == 2 else "UNKNOWN",
            "volume_units": volume_raw / 100,
            "entry_price": entry_price,
            "stop_loss": stop_loss if stop_loss is not None and math.isfinite(stop_loss) else None,
            "take_profit": take_profit if take_profit is not None and math.isfinite(take_profit) else None,
            "net_unrealized_pnl": pnl_by_position.get(position_id),
            "pnl_currency_digits": pnl_money_digits,
            "opened_at": trade_data.get("openTimestamp"),
            "last_updated_at": item.get("utcLastUpdateTimestamp"),
            "used_margin": item.get("usedMargin"),
        })

    pending_orders: list[dict[str, Any]] = []
    for item in raw_orders:
        if not isinstance(item, dict):
            continue
        trade_data = item.get("tradeData") if isinstance(item.get("tradeData"), dict) else {}
        order_id = _as_protocol_int(item.get("orderId"))
        symbol_id = _as_protocol_int(trade_data.get("symbolId"))
        volume_raw = _as_protocol_int(trade_data.get("volume"))
        if order_id is None:
            continue
        pending_orders.append({
            "order_id": str(order_id),
            "symbol_id": symbol_id,
            "symbol_name": symbol_names.get(symbol_id, f"Symbol {symbol_id}" if symbol_id is not None else "Unknown symbol"),
            "side": "BUY" if _as_protocol_int(trade_data.get("tradeSide")) == 1 else "SELL",
            "volume_units": volume_raw / 100 if volume_raw is not None else None,
            "order_type": item.get("orderType"),
            "limit_price": item.get("limitPrice"),
            "stop_price": item.get("stopPrice"),
        })
    return {
        "account_id": account_id,
        "account_type": account_type,
        "positions": positions,
        "pending_orders": pending_orders,
        "pnl_available": pnl_money_digits is not None,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


async def _require_demo_position(
    user_id: str,
    account_id: str,
    position_id: int,
    credential: CTraderCredential,
) -> tuple[dict[str, Any], dict[str, Any]]:
    links = await _load_account_links(user_id)
    account = next((item for item in links if item["account_id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=403, detail="This cTrader account is not linked to your TradeBridge user.")
    if account["account_type"] != "demo":
        raise HTTPException(status_code=403, detail="Position actions are locked for live accounts.")
    if credential.permission_scope != 1:
        raise HTTPException(status_code=403, detail="Trading permission was not granted. Reconnect and approve trading access.")
    snapshot = await _provider_reconcile(credential, account_id, False)
    position = _open_position_by_id(snapshot, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="The position is no longer open. Refresh the positions list.")
    return account, position


async def _validate_position_action_response(
    user_id: str,
    idempotency_key: str,
    payload_type: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    execution_type = _execution_type_value(payload)
    if payload.get("errorCode") or execution_type == 7:
        error_code = str(payload.get("errorCode") or "ORDER_REJECTED")
        await _update_position_action(user_id, idempotency_key, "rejected", error_code)
        raise HTTPException(status_code=422, detail="cTrader rejected the position action: " + error_code)
    position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
    position_id = _optional_protocol_id(position.get("positionId"))
    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    order_id = _optional_protocol_id(order.get("orderId"))
    if payload_type != EXECUTION_EVENT:
        await _update_position_action(user_id, idempotency_key, "unknown")
        raise HTTPException(status_code=504, detail="cTrader returned an unexpected action acknowledgement. Refresh positions before proceeding.")
    await _update_position_action(user_id, idempotency_key, "completed")
    return {
        "status": "acknowledged",
        "position_id": str(position_id) if position_id is not None else None,
        "order_id": str(order_id) if order_id is not None else None,
        "message": "cTrader acknowledged the position action. Refresh positions to verify the resulting state.",
    }


async def _refresh_credential(user_id: str, credential: CTraderCredential) -> CTraderCredential:
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
    return await _store_credential(user_id, access_token, refresh_token, expires_in, credential.permission_scope)


async def _owned_credential(user_id: str) -> CTraderCredential:
    credential = await _load_credential(user_id)
    if credential is None:
        raise HTTPException(status_code=409, detail="cTrader is not connected to this TradeBridge account.")
    if credential.expires_at <= time.time() + 120:
        return await _refresh_credential(user_id, credential)
    return credential


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
        if response_type == ERROR_RES or response_payload.get("errorCode"):
            raise CTraderProviderError(str(response_payload.get("errorCode", "PROVIDER_ERROR")), str(response_payload.get("description", "")))
        if response_type not in expected_types:
            raise CTraderProviderError("UNEXPECTED_RESPONSE", "cTrader returned an unexpected response.")
        return response_type, response_payload


async def _connect_and_authorize_application(websocket: Any) -> None:
    client_id, client_secret = _ctrader_app_credentials()
    await _send_and_wait(
        websocket, APP_AUTH_REQ, {"clientId": client_id, "clientSecret": client_secret}, {APP_AUTH_RES}
    )


async def _list_accounts_on_host(host: str, access_token: str) -> tuple[list[dict[str, Any]], int]:
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
    raw_scope = payload.get("permissionScope", 0)
    permission_scope = 1 if raw_scope in (1, "1", "SCOPE_TRADE", "trading") else 0
    candidates = payload.get("ctidTraderAccount", [])
    if not isinstance(candidates, list):
        return [], permission_scope
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
    return accounts, permission_scope


async def _list_authorized_accounts(access_token: str) -> tuple[list[dict[str, Any]], int]:
    accounts: dict[str, dict[str, Any]] = {}
    failures: list[Exception] = []
    permission_scope = 0
    for host in (DEMO_HOST, LIVE_HOST):
        try:
            found, scope = await _list_accounts_on_host(host, access_token)
            permission_scope = max(permission_scope, scope)
            for account in found:
                accounts[account["account_id"]] = account
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException, CTraderProviderError) as exc:
            failures.append(exc)
    if not accounts and failures:
        raise HTTPException(status_code=502, detail="Could not verify cTrader accounts through Open API.")
    return list(accounts.values()), permission_scope


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


async def _fetch_spot_quote(
    credential: CTraderCredential,
    account_id: str,
    is_live: bool,
    symbol_id: int,
) -> dict[str, Any]:
    """Retrieve one real bid/ask snapshot from the linked cTrader account's venue."""
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
        await _send_and_wait(
            websocket,
            SPOTS_SUBSCRIBE_REQ,
            {
                "ctidTraderAccountId": int(account_id),
                "symbolId": [symbol_id],
                "subscribeToSpotTimestamp": True,
            },
            {SPOTS_SUBSCRIBE_RES},
        )
        deadline = time.monotonic() + 8.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("cTrader did not provide a bid/ask snapshot.")
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            try:
                payload_type = int(message.get("payloadType"))
            except (TypeError, ValueError):
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload_type == ERROR_RES:
                raise CTraderProviderError(
                    str(payload.get("errorCode", "PROVIDER_ERROR")),
                    str(payload.get("description", "")),
                )
            if payload_type != SPOT_EVENT:
                continue
            if str(payload.get("ctidTraderAccountId", "")) != account_id:
                continue
            if str(payload.get("symbolId", "")) != str(symbol_id):
                continue
            try:
                bid_raw = float(payload.get("bid"))
                ask_raw = float(payload.get("ask"))
            except (TypeError, ValueError):
                continue
            if not (0 < bid_raw < float("inf") and 0 < ask_raw < float("inf") and ask_raw >= bid_raw):
                continue
            stamp = _numeric_timestamp(payload.get("timestamp"))
            return {
                "bid_raw": bid_raw,
                "ask_raw": ask_raw,
                "provider_timestamp": stamp,
            }


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
    try:
        user_id = str(uuid.UUID(user_id))
    except (ValueError, AttributeError):
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

    accounts, permission_scope = await _list_authorized_accounts(access_token)
    if not accounts:
        raise HTTPException(status_code=502, detail="No cTrader accounts were authorized. Reconnect and choose an account.")
    # Store ownership links only after Open API verifies both the account list and granted scope.
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


@router.get("/quote")
async def quote(
    user_id: Annotated[str, Depends(current_user)],
    account_id: str = Query(min_length=1, max_length=24),
    symbol_id: int = Query(gt=0),
) -> dict[str, Any]:
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        raise HTTPException(status_code=422, detail="Invalid cTrader account identifier.")
    credential = await _owned_credential(user_id)
    links = await _load_account_links(user_id)
    account = next((item for item in links if item["account_id"] == account_id), None)
    if not account:
        raise HTTPException(status_code=403, detail="This cTrader account is not linked to your TradeBridge user.")

    try:
        _, light_payload = await _call_account_api(
            credential,
            account_id,
            account["account_type"] == "live",
            SYMBOLS_LIST_REQ,
            {"ctidTraderAccountId": int(account_id), "includeArchivedSymbols": False},
            {SYMBOLS_LIST_RES},
        )
        candidates = light_payload.get("symbol", [])
        selected = next(
            (
                item for item in candidates
                if isinstance(item, dict)
                and str(item.get("symbolId", "")) == str(symbol_id)
                and item.get("enabled") is not False
                and _is_forex_symbol(item.get("symbolName"))
            ),
            None,
        ) if isinstance(candidates, list) else None
        if not selected:
            raise HTTPException(status_code=422, detail="Select an enabled Forex symbol from the linked cTrader account.")
        _, symbol_payload = await _call_account_api(
            credential,
            account_id,
            account["account_type"] == "live",
            SYMBOL_BY_ID_REQ,
            {"ctidTraderAccountId": int(account_id), "symbolId": [symbol_id]},
            {SYMBOL_BY_ID_RES},
        )
        full_symbols = symbol_payload.get("symbol", [])
        symbol = next(
            (
                item for item in full_symbols
                if isinstance(item, dict) and str(item.get("symbolId", "")) == str(symbol_id)
            ),
            None,
        ) if isinstance(full_symbols, list) else None
        if not symbol:
            raise HTTPException(status_code=502, detail="cTrader did not return symbol price precision.")
        spot = await _fetch_spot_quote(
            credential, account_id, account["account_type"] == "live", symbol_id
        )
    except HTTPException:
        raise
    except CTraderProviderError as exc:
        raise HTTPException(status_code=502, detail="cTrader quote request failed: " + exc.error_code)
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
        raise HTTPException(status_code=502, detail="cTrader did not provide a fresh quote. Refresh before confirming an order.")

    try:
        digits = max(0, min(10, int(symbol.get("digits", 5))))
        pip_position = max(0, min(10, int(symbol.get("pipPosition", 4))))
    except (TypeError, ValueError):
        digits, pip_position = 5, 4
    bid = round(spot["bid_raw"] / 100_000, digits)
    ask = round(spot["ask_raw"] / 100_000, digits)
    spread = round(ask - bid, digits)
    provider_timestamp = _numeric_timestamp(spot.get("provider_timestamp"))
    provider_quote_at: str | None = None
    quote_age_seconds: float | None = None
    if provider_timestamp is not None:
        # The provider serializes spot timestamps as Unix epoch values. Accept
        # either seconds or milliseconds, since account gateways may differ.
        timestamp_seconds = float(provider_timestamp) / 1000 if provider_timestamp > 10_000_000_000 else float(provider_timestamp)
        try:
            provider_quote_at = datetime.fromtimestamp(timestamp_seconds, timezone.utc).isoformat()
            quote_age_seconds = round(max(0.0, time.time() - timestamp_seconds), 3)
        except (ValueError, OverflowError, OSError):
            provider_quote_at = None
            quote_age_seconds = None
    if quote_age_seconds is None or quote_age_seconds > 15:
        raise HTTPException(
            status_code=409,
            detail="The cTrader price snapshot is stale or missing its provider timestamp. Wait for a fresh quote before trading.",
        )
    return {
        "account_id": account_id,
        "account_type": account["account_type"],
        "symbol_id": symbol_id,
        "symbol_name": selected.get("symbolName"),
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "spread_pips": round(spread * (10 ** pip_position), 2),
        "digits": digits,
        "pip_position": pip_position,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "provider_timestamp": provider_timestamp,
        "provider_quote_at": provider_quote_at,
        "quote_age_seconds": quote_age_seconds,
    }


@router.get("/positions")
async def list_positions(
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
        return await _position_snapshot(user_id, account_id, account["account_type"], credential)
    except HTTPException:
        raise
    except CTraderProviderError as exc:
        raise HTTPException(status_code=502, detail="cTrader positions request failed: " + exc.error_code)
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
        raise HTTPException(status_code=502, detail="Could not retrieve positions from cTrader.")


@router.post("/positions/{position_id}/protection")
async def amend_position_protection(
    position_id: str,
    body: CTraderProtectionRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    if not POSITION_ID_RE.fullmatch(position_id):
        raise HTTPException(status_code=422, detail="Invalid cTrader position identifier.")
    if body.confirmed is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required for a position-protection change.")
    supplied = body.model_fields_set.intersection({"stop_loss", "take_profit"})
    if not supplied:
        raise HTTPException(status_code=422, detail="Supply a new stop-loss and/or take-profit price.")
    if any(getattr(body, field) is None for field in supplied):
        raise HTTPException(status_code=422, detail="Protection prices cannot be cleared in this version; enter positive prices instead.")
    parsed_position_id = int(position_id)
    credential = await _owned_credential(user_id)
    _, position = await _require_demo_position(user_id, body.account_id, parsed_position_id, credential)
    action_payload = {
        "account_id": body.account_id,
        "position_id": parsed_position_id,
        "stop_loss": body.stop_loss if "stop_loss" in supplied else None,
        "take_profit": body.take_profit if "take_profit" in supplied else None,
    }
    request_hash = hashlib.sha256(json.dumps(action_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    await _claim_position_action(user_id, body.idempotency_key, body.account_id, parsed_position_id, "protection", request_hash)
    request_payload: dict[str, Any] = {
        "ctidTraderAccountId": int(body.account_id),
        "positionId": parsed_position_id,
    }
    if "stop_loss" in supplied:
        request_payload["stopLoss"] = body.stop_loss
    if "take_profit" in supplied:
        request_payload["takeProfit"] = body.take_profit
    try:
        payload_type, response_payload = await _call_account_api(
            credential,
            body.account_id,
            False,
            AMEND_POSITION_SLTP_REQ,
            request_payload,
            {EXECUTION_EVENT, ORDER_ERROR_EVENT},
            timeout_seconds=12.0,
        )
        result = await _validate_position_action_response(user_id, body.idempotency_key, payload_type, response_payload)
    except CTraderProviderError as exc:
        await _update_position_action(user_id, body.idempotency_key, "rejected", exc.error_code)
        raise HTTPException(status_code=422, detail="cTrader rejected the protection change: " + exc.error_code)
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
        await _update_position_action(user_id, body.idempotency_key, "unknown")
        raise HTTPException(status_code=504, detail="The protection-change outcome is unknown. Refresh positions in cTrader before retrying.")
    return {**result, "account_id": body.account_id, "position_id": position_id}


@router.post("/positions/{position_id}/close")
async def close_position(
    position_id: str,
    body: CTraderClosePositionRequest,
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    if not POSITION_ID_RE.fullmatch(position_id):
        raise HTTPException(status_code=422, detail="Invalid cTrader position identifier.")
    if body.confirmed is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required to close a position.")
    parsed_position_id = int(position_id)
    credential = await _owned_credential(user_id)
    _, position = await _require_demo_position(user_id, body.account_id, parsed_position_id, credential)
    trade_data = position.get("tradeData") if isinstance(position.get("tradeData"), dict) else {}
    current_volume = _as_protocol_int(trade_data.get("volume"))
    symbol_id = _as_protocol_int(trade_data.get("symbolId"))
    if current_volume is None or current_volume <= 0 or symbol_id is None:
        raise HTTPException(status_code=502, detail="cTrader position is missing valid volume or symbol information.")
    if body.volume_units is None:
        close_volume = current_volume
    else:
        requested_cents = int(round(body.volume_units * 100))
        if requested_cents >= current_volume:
            raise HTTPException(status_code=422, detail="For a full close, leave close volume blank. Partial-close volume must be smaller than the position.")
        try:
            _, full_payload = await _call_account_api(
                credential,
                body.account_id,
                False,
                SYMBOL_BY_ID_REQ,
                {"ctidTraderAccountId": int(body.account_id), "symbolId": [symbol_id]},
                {SYMBOL_BY_ID_RES},
            )
        except (CTraderProviderError, TimeoutError, OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
            raise HTTPException(status_code=502, detail="Could not validate the cTrader partial-close size.")
        full_symbols = full_payload.get("symbol", [])
        symbol = next((item for item in full_symbols if isinstance(item, dict) and _as_protocol_int(item.get("symbolId")) == symbol_id), None) if isinstance(full_symbols, list) else None
        if not symbol:
            raise HTTPException(status_code=502, detail="cTrader did not return the symbol's volume rules.")
        min_volume = _as_protocol_int(symbol.get("minVolume")) or 0
        step_volume = _as_protocol_int(symbol.get("stepVolume")) or 0
        if min_volume <= 0 or step_volume <= 0 or requested_cents < min_volume or (requested_cents - min_volume) % step_volume != 0:
            raise HTTPException(status_code=422, detail="Partial-close volume must follow the minimum and step allowed for this cTrader symbol.")
        close_volume = requested_cents

    action_payload = {
        "account_id": body.account_id,
        "position_id": parsed_position_id,
        "volume_cents": close_volume,
    }
    request_hash = hashlib.sha256(json.dumps(action_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    await _claim_position_action(user_id, body.idempotency_key, body.account_id, parsed_position_id, "close", request_hash)
    try:
        payload_type, response_payload = await _call_account_api(
            credential,
            body.account_id,
            False,
            CLOSE_POSITION_REQ,
            {
                "ctidTraderAccountId": int(body.account_id),
                "positionId": parsed_position_id,
                "volume": close_volume,
            },
            {EXECUTION_EVENT, ORDER_ERROR_EVENT},
            timeout_seconds=12.0,
        )
        result = await _validate_position_action_response(user_id, body.idempotency_key, payload_type, response_payload)
    except CTraderProviderError as exc:
        await _update_position_action(user_id, body.idempotency_key, "rejected", exc.error_code)
        raise HTTPException(status_code=422, detail="cTrader rejected the close request: " + exc.error_code)
    except (TimeoutError, OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError):
        await _update_position_action(user_id, body.idempotency_key, "unknown")
        raise HTTPException(status_code=504, detail="The close outcome is unknown. Refresh positions in cTrader before attempting another action.")
    return {
        **result,
        "account_id": body.account_id,
        "position_id": position_id,
        "closed_volume_units": close_volume / 100,
        "full_close": close_volume == current_volume,
    }



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
        max_volume = min(100_000.0, max(0.01, float(os.getenv("CTRADER_DEMO_MAX_VOLUME_UNITS", "1000"))))
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

    execution_type = _execution_type_value(payload)
    if payload.get("errorCode") or execution_type == 7:
        error_code = str(payload.get("errorCode") or "ORDER_REJECTED")
        await _update_order_request(user_id, body.idempotency_key, "rejected", provider_error=error_code)
        raise HTTPException(status_code=422, detail="cTrader rejected the demo order: " + error_code)

    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    position = payload.get("position") if isinstance(payload.get("position"), dict) else {}
    order_id = _optional_protocol_id(order.get("orderId"))
    position_id = _optional_protocol_id(position.get("positionId"))
    status_by_execution = {2: "accepted", 3: "filled", 11: "partial"}
    order_status = status_by_execution.get(execution_type)
    if order_status is None:
        await _update_order_request(user_id, body.idempotency_key, "unknown")
        raise HTTPException(
            status_code=504,
            detail="cTrader returned an unrecognized execution state. The outcome is unknown; do not retry this order.",
        )

    await _update_order_request(user_id, body.idempotency_key, order_status, order_id, position_id)
    if order_status == "filled":
        order_message = "cTrader reports the demo order filled. Verify the resulting position in account activity."
    elif order_status == "partial":
        order_message = "cTrader reports a partial fill. Check the position and remaining order amount in account activity."
    else:
        order_message = "cTrader accepted the demo order request. Verify the final fill and position in account activity."
    return {
        "status": order_status,
        "account_id": body.account_id,
        "symbol_id": body.symbol_id,
        "side": body.side,
        "volume_units": body.volume_units,
        "order_id": str(order_id) if order_id is not None else None,
        "position_id": str(position_id) if position_id is not None else None,
        "message": order_message,
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
