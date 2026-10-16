"""Server-owned Deriv credential custody and account-session bootstrap.

Credentials are held in process memory only. This intentionally requires an
always-on ASGI deployment with one process until a shared encrypted session store
and distributed ownership/locking are implemented. Never log access tokens.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr, StrictStr
import websockets

router = APIRouter(prefix="/deriv", tags=["deriv"])
bearer = HTTPBearer(auto_error=False)
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9]{2,32}$")
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")
_credentials: dict[str, "Credential"] = {}
_ws_tickets: dict[str, tuple[str, float]] = {}


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


def _origin_allowed(origin: str | None) -> bool:
    configured = {
        value.strip().rstrip("/")
        for value in os.getenv("TRADEBRIDGE_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    }
    # If configured, browser WebSocket handshakes must originate from one of
    # these exact origins. Native clients without Origin remain supported.
    return not origin or not configured or origin.rstrip("/") in configured


def _require_bridge_secret(supplied: str | None) -> None:
    expected = os.getenv("TRADEBRIDGE_SESSION_BRIDGE_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Credential bridge is not configured.")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized credential transfer.")


async def _resolve_user_token(token: str) -> str:
    """Validate a Supabase access token without exposing it to logs or responses."""
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


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    return await _resolve_user_token(credentials.credentials if credentials else "")


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


async def _linked_account_ids(user_id: str, account_ids: list[str]) -> set[str]:
    """Return only account IDs linked to this app user; fail closed on DB errors."""
    if not account_ids:
        return set()
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=503, detail="Secure account linking is not configured.")
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            response = await client.get(
                f"{supabase_url}/rest/v1/deriv_account_links",
                params={
                    "select": "deriv_account_id",
                    "user_id": f"eq.{user_id}",
                    "deriv_account_id": f"in.({','.join(account_ids)})",
                },
                headers={"apikey": service_key, "Authorization": f"Bearer {service_key}"},
            )
        if not response.is_success:
            raise HTTPException(status_code=503, detail="Secure account linking could not be verified.")
        rows = response.json()
        if not isinstance(rows, list):
            raise HTTPException(status_code=503, detail="Secure account linking returned invalid data.")
        return {
            row["deriv_account_id"]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("deriv_account_id"), str)
        }
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Secure account linking could not be verified.")


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
        if balance is not None and not __import__("math").isfinite(balance):
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
                _credentials.pop(user_id, None)
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


_ALLOWED_CLIENT_REQUESTS = {
    "active_symbols", "ticks", "ticks_history", "proposal", "buy", "sell",
    "balance", "portfolio", "statement", "profit_table", "proposal_open_contract",
    "transaction", "ping", "forget", "forget_all",
}


@router.post("/ws-ticket")
async def create_websocket_ticket(
    user_id: Annotated[str, Depends(current_user)],
) -> dict[str, Any]:
    """Issue a short-lived, one-use WebSocket ticket instead of putting a JWT in handshake headers."""
    now = time.time()
    for ticket, (_, expires_at) in list(_ws_tickets.items()):
        if expires_at <= now:
            _ws_tickets.pop(ticket, None)
    ticket = secrets.token_urlsafe(32)
    _ws_tickets[ticket] = (user_id, now + 30)
    return {"ticket": ticket, "expires_in": 30}


@router.websocket("/ws")
async def deriv_websocket_proxy(websocket: WebSocket) -> None:
    """Proxy an authenticated demo-account session using a one-use short-lived ticket."""
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    offered = [part.strip() for part in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    ticket = next((part for part in offered if part != "tradebridge"), "")
    ticket_record = _ws_tickets.pop(ticket, None)
    if not ticket_record or ticket_record[1] <= time.time():
        await websocket.close(code=4401)
        return
    user_id = ticket_record[0]

    account_id = websocket.query_params.get("account_id", "")
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        await websocket.close(code=4400)
        return
    try:
        credential = await _owned_credential(user_id)
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code in (401, 409) else 1011)
        return

    # Validate the account against the OAuth credential, and intentionally keep
    # real-account execution disabled at the backend boundary.
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            accounts_response = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
            )
            if accounts_response.status_code == 401:
                _credentials.pop(user_id, None)
                await websocket.close(code=4401)
                return
            if not accounts_response.is_success:
                await websocket.close(code=1011)
                return
            payload = accounts_response.json()
            root = payload if isinstance(payload, dict) else {}
            data = root.get("data")
            accounts = (
                data if isinstance(data, list)
                else data.get("accounts", []) if isinstance(data, dict) and isinstance(data.get("accounts", []), list)
                else [data] if isinstance(data, dict) and data.get("account_id")
                else root.get("accounts", []) if isinstance(root.get("accounts"), list)
                else []
            )
            account = next((item for item in accounts if isinstance(item, dict) and item.get("account_id") == account_id), None)
            if not account or str(account.get("account_type", "")).lower() != "demo":
                await websocket.close(code=4403)
                return
            otp_response = await client.post(
                f"https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp",
                headers={"Authorization": f"Bearer {credential.access_token}", "Accept": "application/json"},
            )
            if not otp_response.is_success:
                await websocket.close(code=1011)
                return
            otp_payload = otp_response.json()
            otp_data = otp_payload.get("data") if isinstance(otp_payload, dict) else None
            provider_url = otp_data.get("url") if isinstance(otp_data, dict) else None
            if not isinstance(provider_url, str):
                await websocket.close(code=1011)
                return
            from urllib.parse import urlparse
            parsed = urlparse(provider_url)
            if parsed.scheme != "wss" or parsed.hostname != "api.derivws.com" or parsed.path != "/trading/v1/options/ws/demo":
                await websocket.close(code=1011)
                return
    except (httpx.HTTPError, ValueError):
        await websocket.close(code=1011)
        return

    try:
        async with websockets.connect(
            provider_url,
            open_timeout=8,
            close_timeout=3,
            ping_interval=20,
            max_size=1_048_576,
        ) as upstream:
            await websocket.accept(subprotocol="tradebridge")

            async def client_to_deriv() -> None:
                while True:
                    raw = await websocket.receive_text()
                    if len(raw) > 256_000:
                        await websocket.close(code=4400)
                        return
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        await websocket.send_json({"error": {"code": "INVALID_JSON", "message": "Send a valid JSON request."}})
                        continue
                    if not isinstance(message, dict):
                        await websocket.send_json({"error": {"code": "UNSUPPORTED_REQUEST", "message": "Send a single Deriv request object."}})
                        continue
                    request_keys = set(message).intersection(_ALLOWED_CLIENT_REQUESTS)
                    if len(request_keys) != 1 or "authorize" in message:
                        await websocket.send_json({"error": {"code": "UNSUPPORTED_REQUEST", "message": "Send exactly one supported Deriv request."}})
                        continue

                    # Enforce the supported manual digit-contract scope at the gateway,
                    # not only in the UI. This endpoint is demo-only.
                    if "proposal" in message:
                        amount = message.get("amount")
                        contract_type = message.get("contract_type")
                        duration = message.get("duration")
                        duration_unit = message.get("duration_unit")
                        symbol = message.get("symbol")
                        allowed_contracts = {"DIGITOVER", "DIGITUNDER", "DIGITEVEN", "DIGITODD", "DIGITMATCH", "DIGITDIFF"}
                        try:
                            stake = float(amount)
                        except (TypeError, ValueError):
                            stake = 0
                        if not (1 <= stake <= float(os.getenv("TRADEBRIDGE_DEMO_MAX_STAKE", "1000"))):
                            await websocket.send_json({"error": {"code": "STAKE_LIMIT", "message": "Demo stake is outside the server-configured limit."}})
                            continue
                        if contract_type not in allowed_contracts or not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
                            await websocket.send_json({"error": {"code": "INVALID_CONTRACT", "message": "Only supported digit contracts and valid symbols are allowed."}})
                            continue
                        if duration_unit != "t" or type(duration) is not int or not 1 <= duration <= 10:
                            await websocket.send_json({"error": {"code": "INVALID_DURATION", "message": "Digit contracts must use 1–10 ticks."}})
                            continue
                        if contract_type in {"DIGITOVER", "DIGITUNDER", "DIGITMATCH", "DIGITDIFF"}:
                            barrier = message.get("barrier")
                            if not isinstance(barrier, (str, int)) or not str(barrier).isdigit() or not 0 <= int(barrier) <= 9:
                                await websocket.send_json({"error": {"code": "INVALID_BARRIER", "message": "A valid digit barrier is required."}})
                                continue
                            if contract_type == "DIGITOVER" and int(barrier) > 8 or contract_type == "DIGITUNDER" and int(barrier) < 1:
                                await websocket.send_json({"error": {"code": "INVALID_BARRIER", "message": "The barrier is outside the contract's valid range."}})
                                continue
                    if "buy" in message:
                        try:
                            buy_price = float(message.get("price"))
                        except (TypeError, ValueError):
                            buy_price = 0
                        if not (0 < buy_price <= float(os.getenv("TRADEBRIDGE_DEMO_MAX_STAKE", "1000"))):
                            await websocket.send_json({"error": {"code": "STAKE_LIMIT", "message": "Purchase price is outside the server-configured limit."}})
                            continue
                        if not isinstance(message.get("buy"), (str, int)) or not str(message.get("buy")).strip():
                            await websocket.send_json({"error": {"code": "INVALID_PROPOSAL", "message": "A valid proposal identifier is required."}})
                            continue
                    if "sell" in message:
                        if not str(message.get("sell", "")).isdigit() or message.get("price") != 0:
                            await websocket.send_json({"error": {"code": "INVALID_SELL", "message": "Only a market sale of an open contract is supported."}})
                            continue
                    await upstream.send(json.dumps(message, separators=(",", ":")))

            async def deriv_to_client() -> None:
                async for raw in upstream:
                    await websocket.send_text(raw)

            tasks = {
                asyncio.create_task(client_to_deriv()),
                asyncio.create_task(deriv_to_client()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled():
                    error = task.exception()
                    if error and not isinstance(error, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)):
                        raise error
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        pass
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        _credentials.get(user_id)


@router.websocket("/live/market")
async def live_market_proxy(websocket: WebSocket) -> None:
    """Proxy public market ticks/history in memory; never persist live market data."""
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    subscribed_symbols: set[str] = set()
    allowed = {"active_symbols", "ticks", "ticks_history", "forget", "forget_all", "ping"}
    try:
        async with websockets.connect(
            "wss://api.derivws.com/trading/v1/options/ws/public",
            open_timeout=8,
            close_timeout=3,
            ping_interval=20,
            max_size=1_048_576,
        ) as upstream:
            async def client_to_upstream() -> None:
                while True:
                    raw = await websocket.receive_text()
                    if len(raw.encode("utf-8")) > 64_000:
                        await websocket.close(code=4400, reason="Request too large")
                        return
                    try:
                        message = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        await websocket.send_json({"error": {"code": "INVALID_JSON", "message": "Send valid JSON."}})
                        continue
                    if not isinstance(message, dict) or not set(message).intersection(allowed):
                        await websocket.send_json({"error": {"code": "UNSUPPORTED_REQUEST", "message": "Unsupported market-data request."}})
                        continue
                    if "ticks" in message or "ticks_history" in message:
                        symbol = message.get("ticks") if isinstance(message.get("ticks"), str) else message.get("ticks_history")
                        if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
                            await websocket.send_json({"error": {"code": "INVALID_SYMBOL", "message": "Invalid market symbol."}})
                            continue
                        if message.get("subscribe") == 1:
                            subscribed_symbols.add(symbol)
                            if len(subscribed_symbols) > 256:
                                subscribed_symbols.discard(symbol)
                                await websocket.send_json({"error": {"code": "SUBSCRIPTION_LIMIT", "message": "Too many active subscriptions."}})
                                continue
                    await upstream.send(json.dumps(message, separators=(",", ":")))

            async def upstream_to_client() -> None:
                async for raw in upstream:
                    await websocket.send_text(raw)

            tasks = {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled():
                    error = task.exception()
                    if error and not isinstance(error, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)):
                        raise error
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        pass
    except Exception:
        try:
            await websocket.close(code=1011, reason="Market feed unavailable")
        except Exception:
            pass
