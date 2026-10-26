"""Authenticated demo-trading WebSocket proxy (reconnect loop + OTP orchestration)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.deriv import router
from app.deriv.client_handler import handle_client_messages
import app.deriv.credentials as _creds
from app.deriv.credentials import _linked_account_ids, _owned_credential
from app.deriv.models import ACCOUNT_ID_RE
from app.deriv.provider_handler import handle_provider_messages
from app.deriv.security import _origin_allowed, _ws_tickets
from app.deriv.trading_session import TradingProxySession

_log = logging.getLogger(__name__)

_ALLOWED_CLIENT_REQUESTS = {
    "active_symbols", "contracts_for", "ticks", "ticks_history", "proposal", "buy", "sell",
    "balance", "portfolio", "statement", "profit_table", "proposal_open_contract",
    "transaction", "ping", "forget", "forget_all",
}


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
                await _creds._delete_credential(user_id)
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
            linked_ids = await _linked_account_ids(user_id, [account_id])
            if account_id not in linked_ids:
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
            parsed = urlparse(provider_url)
            if parsed.scheme != "wss" or parsed.hostname != "api.derivws.com" or parsed.path != "/trading/v1/options/ws/demo":
                await websocket.close(code=1011)
                return
    except HTTPException as exc:
        await websocket.close(code=4403 if exc.status_code in (403, 409) else 1011)
        return
    except (httpx.HTTPError, ValueError):
        await websocket.close(code=1011)
        return

    _MAX_RECONNECT_ATTEMPTS = 5
    reconnect_attempt = 0
    session = TradingProxySession(allowed_client_requests=_ALLOWED_CLIENT_REQUESTS)

    while True:
        # Re-fetch OTP on every connect attempt (OTPs are single-use).
        try:
            credential = await _owned_credential(user_id)
        except HTTPException as exc:
            await websocket.close(code=4401 if exc.status_code in (401, 409) else 1011)
            return
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
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
                reconnect_attempt = 0  # reset on successful connection
                if websocket.client_state == WebSocketState.CONNECTING:
                    await websocket.accept(subprotocol="tradebridge")

                # Resubscribe tick stream after a reconnect.
                await session.resubscribe_ticks(upstream)

                client_task = asyncio.create_task(handle_client_messages(session, upstream, websocket))
                tasks = {
                    client_task,
                    asyncio.create_task(handle_provider_messages(session, upstream, websocket)),
                    asyncio.create_task(session.strategy_response_timeout_monitor(websocket, upstream)),
                    asyncio.create_task(session.credential_expiry_monitor(websocket, user_id)),
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    if not task.cancelled():
                        error = task.exception()
                        if error and not isinstance(error, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)):
                            raise error
                # Client disconnected cleanly — stop.
                if client_task in done:
                    return

                # Otherwise upstream closed; trigger reconnect loop.
                raise websockets.exceptions.ConnectionClosed(None, None)

        except WebSocketDisconnect:
            return
        except websockets.exceptions.ConnectionClosed:
            reconnect_attempt += 1
            if reconnect_attempt > _MAX_RECONNECT_ATTEMPTS:
                _log.warning("deriv_websocket_proxy: upstream closed after %d reconnect attempts; giving up.", _MAX_RECONNECT_ATTEMPTS)
                try:
                    await websocket.send_json({"error": {"code": "SESSION_DISCONNECTED", "message": "Deriv connection lost. Please reconnect."}})
                    await websocket.close(code=1011)
                except Exception:
                    pass
                return
            delay = min(2 ** (reconnect_attempt - 1), 30) + random.uniform(0, 1)
            _log.info("deriv_websocket_proxy: upstream closed, reconnecting in %.1fs (attempt %d/%d).", delay, reconnect_attempt, _MAX_RECONNECT_ATTEMPTS)
            await asyncio.sleep(delay)
        except Exception:
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
            return
