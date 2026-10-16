"""Ephemeral Deriv tick relay. No persistence, analytics, or trading actions."""
import json
import os
import re

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")


@router.websocket("/live/ticks")
async def live_ticks(websocket: WebSocket) -> None:
    """Relay public Deriv ticks directly to one browser client; never write to a DB."""
    symbol = websocket.query_params.get("symbol", "1HZ100V")
    app_id = os.getenv("DERIV_APP_ID", "").strip()

    if not SYMBOL_PATTERN.fullmatch(symbol):
        await websocket.close(code=1008, reason="Invalid market symbol")
        return
    if not app_id or not app_id.isdigit():
        await websocket.accept()
        await websocket.send_json({
            "type": "error",
            "code": "deriv_app_id_missing",
            "message": "Live feed is not configured on the server.",
        })
        await websocket.close(code=1011, reason="Live feed is not configured")
        return

    await websocket.accept()
    await websocket.send_json({"type": "status", "status": "connecting", "symbol": symbol})

    upstream_url = f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
    try:
        async with websockets.connect(
            upstream_url,
            open_timeout=10,
            close_timeout=3,
            ping_interval=20,
            ping_timeout=20,
            max_size=1_000_000,
        ) as deriv:
            await deriv.send(json.dumps({"ticks": symbol, "subscribe": 1}))
            await websocket.send_json({"type": "status", "status": "connected", "symbol": symbol})

            async for raw_message in deriv:
                try:
                    payload = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError):
                    continue

                if payload.get("error"):
                    await websocket.send_json({
                        "type": "error",
                        "code": str(payload["error"].get("code", "deriv_error")),
                        "message": "Deriv could not provide this market feed.",
                    })
                    break

                tick = payload.get("tick")
                if not isinstance(tick, dict) or tick.get("symbol") != symbol:
                    continue

                # Forward only the current tick fields required by the live UI.
                await websocket.send_json({
                    "type": "tick",
                    "symbol": symbol,
                    "quote": tick.get("quote"),
                    "epoch": tick.get("epoch"),
                    "pip_size": tick.get("pip_size"),
                })
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.send_json({
                "type": "error",
                "code": "live_feed_disconnected",
                "message": "Live feed disconnected. Reconnect to resume updates.",
            })
            await websocket.close(code=1011, reason="Upstream feed disconnected")
        except Exception:
            pass
