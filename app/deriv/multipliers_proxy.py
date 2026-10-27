"""Authenticated legacy WebSocket proxy for Deriv Multipliers contracts."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

import httpx
import websockets
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from app.deriv import router
from app.deriv.client_handler import handle_client_messages
from app.deriv.credentials import _linked_account_ids, _owned_credential
from app.deriv.models import ACCOUNT_ID_RE
from app.deriv.provider_handler import handle_provider_messages
from app.deriv.security import _origin_allowed, _ws_tickets
from app.deriv.trading_session import TradingProxySession

_ALLOWED_MULTIPLIER_REQUESTS = {
    "active_symbols", "contracts_for", "ticks", "ticks_history", "proposal", "buy", "sell",
    "balance", "portfolio", "statement", "profit_table", "proposal_open_contract",
    "transaction", "ping", "forget", "forget_all",
}
_APP_ID_PATTERN = re.compile(r"^[0-9]{1,12}$")


async def _selected_account_type(user_id: str, account_id: str, access_token: str) -> str:
    """Return the linked Options account type, verifying it against Deriv."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        if response.status_code == 401:
            raise HTTPException(status_code=401, detail="Deriv authorization expired; reconnect.")
        if not response.is_success:
            raise HTTPException(status_code=502, detail="Deriv account verification failed.")
        payload = response.json()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Deriv account verification failed.")

    root = payload if isinstance(payload, dict) else {}
    data = root.get("data")
    accounts = (
        data if isinstance(data, list)
        else data.get("accounts", []) if isinstance(data, dict) and isinstance(data.get("accounts", []), list)
        else [data] if isinstance(data, dict) and data.get("account_id")
        else root.get("accounts", []) if isinstance(root.get("accounts"), list)
        else []
    )
    account = next(
        (item for item in accounts if isinstance(item, dict) and item.get("account_id") == account_id),
        None,
    )
    account_type = str(account.get("account_type", "")).lower() if account else ""
    if account_type not in {"demo", "real"}:
        raise HTTPException(status_code=403, detail="Selected account is not a supported Options account.")
    linked_ids = await _linked_account_ids(user_id, [account_id])
    if account_id not in linked_ids:
        raise HTTPException(status_code=403, detail="Selected account is not linked to this TradeBridge user.")
    return account_type


def _authorized_login_matches(authorized: object, account_id: str) -> bool:
    """Bind the legacy authorization token to the exact selected account."""
    return isinstance(authorized, dict) and authorized.get("loginid") == account_id


async def _keepalive(upstream: websockets.ClientConnection) -> None:
    while True:
        await asyncio.sleep(30)
        await upstream.send(json.dumps({"ping": 1}, separators=(",", ":")))


@router.websocket("/multipliers/ws")
async def deriv_multipliers_websocket_proxy(websocket: WebSocket) -> None:
    """Proxy an owned selected account to Deriv's legacy trading WebSocket."""
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
        account_type = await _selected_account_type(user_id, account_id, credential.access_token)
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code in (401, 409) else 4403)
        return

    app_id = os.getenv("DERIV_APP_ID", "").strip()
    if not _APP_ID_PATTERN.fullmatch(app_id):
        await websocket.close(code=1011, reason="Multipliers app ID is not configured.")
        return

    await websocket.accept(subprotocol="tradebridge")
    upstream_url = f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
    try:
        async with websockets.connect(
            upstream_url,
            open_timeout=8,
            close_timeout=3,
            ping_interval=20,
            max_size=1_048_576,
        ) as upstream:
            await upstream.send(json.dumps({"authorize": credential.access_token, "req_id": 1}, separators=(",", ":")))
            try:
                authorization_raw = await asyncio.wait_for(upstream.recv(), timeout=10)
                authorization = json.loads(authorization_raw)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed, json.JSONDecodeError, TypeError):
                await websocket.send_json({"error": {"code": "DERIV_AUTHORIZE_FAILED", "message": "Deriv authorization did not return a valid response."}})
                await websocket.close(code=4403)
                return

            if not isinstance(authorization, dict) or authorization.get("msg_type") != "authorize":
                await websocket.send_json({"error": {"code": "DERIV_AUTHORIZE_FAILED", "message": "Deriv rejected the Multipliers authorization."}})
                await websocket.close(code=4403)
                return
            if not _authorized_login_matches(authorization.get("authorize"), account_id):
                await websocket.send_json({"error": {"code": "DERIV_ACCOUNT_MISMATCH", "message": "The authorized Deriv login does not match the selected account. Select the matching account and reconnect."}})
                await websocket.close(code=4403)
                return

            session = TradingProxySession(
                allowed_client_requests=_ALLOWED_MULTIPLIER_REQUESTS,
                account_type=account_type,
                product_api="multipliers",
            )
            await websocket.send_json({
                "type": "multipliers_connection_status",
                "connected": True,
                "account_id": account_id,
                "account_type": account_type,
                "message": "Authenticated Multipliers WebSocket ready.",
            })

            client_task = asyncio.create_task(handle_client_messages(session, upstream, websocket))
            provider_task = asyncio.create_task(handle_provider_messages(session, upstream, websocket))
            keepalive_task = asyncio.create_task(_keepalive(upstream))
            done, pending = await asyncio.wait(
                {client_task, provider_task, keepalive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                if not task.cancelled():
                    error = task.exception()
                    if error and not isinstance(error, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)):
                        raise error
    except WebSocketDisconnect:
        return
    except websockets.exceptions.ConnectionClosed:
        try:
            await websocket.send_json({"error": {"code": "DERIV_DISCONNECTED", "message": "Deriv Multipliers connection closed. Reconnect to continue."}})
        except Exception:
            pass
    except Exception:
        try:
            await websocket.send_json({"error": {"code": "MULTIPLIERS_CONNECTION_FAILED", "message": "Could not establish the Deriv Multipliers session."}})
            await websocket.close(code=1011)
        except Exception:
            pass