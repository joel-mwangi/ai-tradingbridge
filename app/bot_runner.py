"""Authenticated Demo bot runner WebSocket endpoint.

WS /bots/run?account_id=VRTC123

Auth flow (same as WS /deriv/ws):
  1. Client calls POST /deriv/ws-ticket to get a 30-second one-use ticket.
  2. Client connects here with the ticket in the sec-websocket-protocol header
     (e.g. "tradebridge, <ticket>").
  3. After the connection is accepted, the client sends a JSON DemoBotConfig as
     the first message.
  4. FastAPI opens a Deriv Demo WS using the OTP pattern, then runs run_bot()
     which streams JSON events back to the client.
  5. Client can send {"action": "stop"} at any time to stop after the current trade.

Stream events (JSON):
  {"event": "started",        "config": {...}}
  {"event": "trade_opened",   "contract_id": "...", "stake": 1.0}
  {"event": "trade_settled",  "contract_id": "...", "payout": 1.8, "profit": 0.8,
                               "trade_count": N, "total_profit": X,
                               "consecutive_losses": 0}
  {"event": "error",          "reason": "...", "detail": "..."}
  {"event": "done",           "stop_reason": "...", "trade_count": N, "total_profit": X}
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.bot_executor import BotResult, run_bot
from app.bot_schema import DemoBotConfig
from app.deriv_gateway import (
    ACCOUNT_ID_RE,
    _delete_credential,
    _linked_account_ids,
    _origin_allowed,
    _owned_credential,
    _ws_tickets,
)

router = APIRouter(tags=["bots"])

# In-memory guard: prevents two concurrent bot runs for the same (user, account)
# on the same process instance. Acceptable for single-worker deployments.
_active_runs: set[tuple[str, str]] = set()


@router.websocket("/bots/run")
async def bot_run_websocket(websocket: WebSocket) -> None:
    """Ticket-authenticated inline Demo bot executor. Streams trade events as JSON."""

    # ── 1. Origin check ──────────────────────────────────────────────────────
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return

    # ── 2. One-use ticket from sec-websocket-protocol header ─────────────────
    offered = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    ticket = next((p for p in offered if p != "tradebridge"), "")
    ticket_record = _ws_tickets.pop(ticket, None)
    if not ticket_record or ticket_record[1] <= time.time():
        await websocket.close(code=4401)
        return
    user_id = ticket_record[0]

    # ── 3. account_id from query param ───────────────────────────────────────
    account_id = websocket.query_params.get("account_id", "")
    if not ACCOUNT_ID_RE.fullmatch(account_id):
        await websocket.close(code=4400)
        return

    # ── 4. Prevent concurrent runs for the same (user, account) ─────────────
    run_key = (user_id, account_id)
    if run_key in _active_runs:
        await websocket.close(code=4409)
        return

    # ── 5. Load and validate credential ──────────────────────────────────────
    try:
        credential = await _owned_credential(user_id)
    except Exception:
        await websocket.close(code=4401)
        return

    # ── 6. Verify account is Demo + owned, then get OTP provider URL ─────────
    provider_url: str | None = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            accounts_response = await client.get(
                "https://api.derivws.com/trading/v1/options/accounts",
                headers={
                    "Authorization": f"Bearer {credential.access_token}",
                    "Accept": "application/json",
                },
            )
            if accounts_response.status_code == 401:
                await _delete_credential(user_id)
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
                else data.get("accounts", []) if isinstance(data, dict) and isinstance(data.get("accounts"), list)
                else root.get("accounts", []) if isinstance(root.get("accounts"), list)
                else []
            )
            account = next(
                (a for a in accounts if isinstance(a, dict) and a.get("account_id") == account_id),
                None,
            )
            if not account or str(account.get("account_type", "")).lower() != "demo":
                await websocket.close(code=4403)
                return

            # Currency must come from the verified provider account, never the browser config.
            account_currency = account.get("currency")
            if not isinstance(account_currency, str) or not account_currency.isalpha() or not 3 <= len(account_currency) <= 10:
                await websocket.close(code=1011)
                return
            account_currency = account_currency.upper()

            linked_ids = await _linked_account_ids(user_id, [account_id])
            if account_id not in linked_ids:
                await websocket.close(code=4403)
                return

            otp_response = await client.post(
                f"https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp",
                headers={
                    "Authorization": f"Bearer {credential.access_token}",
                    "Accept": "application/json",
                },
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
            if (
                parsed.scheme != "wss"
                or parsed.hostname != "api.derivws.com"
                or parsed.path != "/trading/v1/options/ws/demo"
            ):
                await websocket.close(code=1011)
                return

    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    # ── 7. Accept the WebSocket and receive the bot config ───────────────────
    await websocket.accept(subprotocol="tradebridge")

    try:
        raw_config = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        config_data = json.loads(raw_config)
        config = DemoBotConfig.model_validate(config_data)
        config.validate_contract()
    except asyncio.TimeoutError:
        await websocket.send_json({"event": "error", "reason": "config_timeout"})
        await websocket.close(code=4400)
        return
    except Exception as exc:
        await websocket.send_json({"event": "error", "reason": "invalid_config", "detail": str(exc)})
        await websocket.close(code=4400)
        return

    # ── 8. Connect to Deriv and run the bot ──────────────────────────────────
    _active_runs.add(run_key)
    stop_signal = asyncio.Event()

    async def send_event(event: dict) -> None:
        """Forward a structured event to the frontend; swallow send errors."""
        try:
            await websocket.send_json(event)
        except Exception:
            pass

    async def listen_for_stop() -> None:
        """Read client messages; set stop_signal on {"action":"stop"} or disconnect."""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict) and msg.get("action") == "stop":
                        stop_signal.set()
                        return
                except (json.JSONDecodeError, TypeError):
                    pass
        except (WebSocketDisconnect, asyncio.CancelledError):
            stop_signal.set()

    result: BotResult | None = None
    try:
        async with websockets.connect(
            provider_url,
            open_timeout=8,
            close_timeout=3,
            ping_interval=20,
            max_size=1_048_576,
        ) as upstream:
            stop_listener = asyncio.create_task(listen_for_stop())
            try:
                result = await run_bot(config, upstream, send_event, stop_signal, currency=account_currency)
            finally:
                stop_listener.cancel()
                try:
                    await stop_listener
                except (asyncio.CancelledError, Exception):
                    pass

        await send_event({
            "event": "done",
            "stop_reason": result.stop_reason if result else "internal_error",
            "trade_count": result.trade_count if result else 0,
            "total_profit": result.total_profit if result else 0.0,
        })
        try:
            await websocket.close(code=1000)
        except Exception:
            pass

    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        pass
    except Exception:
        try:
            await websocket.send_json({"event": "error", "reason": "internal_error"})
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        _active_runs.discard(run_key)
