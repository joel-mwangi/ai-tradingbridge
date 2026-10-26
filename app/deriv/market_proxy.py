"""Public market-data WebSocket proxy (ticks/history) with reconnection.

Self-contained: keeps its own per-connection state as local closures and shares
no state with the authenticated trading proxy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
import time

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app.deriv import router
from app.deriv.security import SYMBOL_PATTERN, _origin_allowed
from app.market_analysis import DigitFrequencyWindow
from app.strategy.live_feed import LiveStrategyFeed


@router.websocket("/live/market")
async def live_market_proxy(websocket: WebSocket) -> None:
    """Proxy public market ticks/history in memory; never persist live market data."""
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    subscribed_symbols: set[str] = set()
    history_requests: dict[int, str] = {}
    frequency_by_symbol: dict[str, DigitFrequencyWindow] = {}
    pip_size_by_symbol: dict[str, int] = {}
    strategy_feed = LiveStrategyFeed()
    allowed = {"active_symbols", "ticks", "ticks_history", "forget", "forget_all", "ping"}
    _log = logging.getLogger(__name__)
    reconnect_attempt = 0
    _MAX_RECONNECT_ATTEMPTS = 5

    while True:
        try:
            async with websockets.connect(
                "wss://api.derivws.com/trading/v1/options/ws/public",
                open_timeout=8,
                close_timeout=3,
                ping_interval=20,
                max_size=1_048_576,
            ) as upstream:
                reconnect_attempt = 0  # reset on successful connection

                # Resubscribe to any symbols that were active before a reconnect.
                for sym in list(subscribed_symbols):
                    try:
                        await upstream.send(json.dumps({"ticks": sym, "subscribe": 1}, separators=(",", ":")))
                    except Exception:
                        pass

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
                        if isinstance(message, dict) and isinstance(message.get("strategy"), dict):
                            control = message["strategy"]
                            action = control.get("action")
                            if action == "start":
                                symbol = control.get("symbol")
                                if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
                                    await websocket.send_json({"error": {"code": "INVALID_STRATEGY_SYMBOL", "message": "Strategy start requires a valid market symbol."}})
                                    continue
                                window_size = control.get("window_size", 1000)
                                if type(window_size) is not int or window_size not in (50, 100, 250, 500, 1000):
                                    await websocket.send_json({"error": {"code": "INVALID_STRATEGY_WINDOW", "message": "Strategy window_size must be 50, 100, 250, 500, or 1000 ticks."}})
                                    continue
                                barrier = control.get("barrier", 5)
                                if type(barrier) is not int or barrier not in range(10):
                                    await websocket.send_json({"error": {"code": "INVALID_STRATEGY_BARRIER", "message": "Strategy barrier must be an integer digit from 0 to 9."}})
                                    continue
                                direction = control.get("direction")
                                if direction not in ("over", "under"):
                                    await websocket.send_json({"error": {"code": "INVALID_STRATEGY_DIRECTION", "message": "Strategy start requires direction 'over' or 'under'."}})
                                    continue
                                already_subscribed = symbol in subscribed_symbols
                                tracker = frequency_by_symbol.setdefault(symbol, DigitFrequencyWindow())
                                initial_frequency = tracker.snapshot(symbol, time.time())
                                try:
                                    status = strategy_feed.start(
                                        symbol,
                                        window_size=window_size,
                                        barrier=barrier,
                                        direction=direction,
                                        initial_frequency=initial_frequency,
                                    )
                                except ValueError as exc:
                                    await websocket.send_json({"error": {"code": "STRATEGY_CONFIG", "message": str(exc)}})
                                    continue
                                subscribed_symbols.add(symbol)
                                if not already_subscribed or len(tracker.points) < window_size:
                                    history_req_id = secrets.randbelow(1_000_000) + 8_000_000
                                    while history_req_id in history_requests:
                                        history_req_id = secrets.randbelow(1_000_000) + 8_000_000
                                    history_requests[history_req_id] = symbol
                                    await upstream.send(json.dumps({"ticks_history": symbol, "count": window_size, "end": "latest", "style": "ticks", "req_id": history_req_id}, separators=(",", ":")))
                                # Do not create a second upstream tick stream when the UI
                                # already subscribed to this symbol or Start is pressed again.
                                if not already_subscribed:
                                    await upstream.send(json.dumps({"ticks": symbol, "subscribe": 1}, separators=(",", ":")))
                                await websocket.send_json(status)
                            elif action == "stop":
                                await websocket.send_json(strategy_feed.stop())
                            elif action == "status":
                                await websocket.send_json(strategy_feed._status("Current strategy observation status."))
                            else:
                                await websocket.send_json({"error": {"code": "INVALID_STRATEGY_ACTION", "message": "Supported strategy actions are start, stop, and status."}})
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
                        if isinstance(message.get("ticks_history"), str) and isinstance(message.get("req_id"), int):
                            history_requests[message["req_id"]] = message["ticks_history"]
                        await upstream.send(json.dumps(message, separators=(",", ":")))

                async def upstream_to_client() -> None:
                    async for raw in upstream:
                        try:
                            provider_message = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            provider_message = None

                        if isinstance(provider_message, dict):
                            msg_type = provider_message.get("msg_type")
                            if msg_type == "history":
                                req_id = provider_message.get("req_id")
                                symbol = history_requests.pop(req_id, None) if isinstance(req_id, int) else None
                                history = provider_message.get("history")
                                if symbol and isinstance(history, dict):
                                    pip_size = provider_message.get("pip_size")
                                    if isinstance(pip_size, int) and not isinstance(pip_size, bool) and 0 <= pip_size <= 12:
                                        pip_size_by_symbol[symbol] = pip_size
                                    tracker = frequency_by_symbol.setdefault(symbol, DigitFrequencyWindow())
                                    tracker.seed_history(
                                        history.get("prices") or [],
                                        history.get("times") or [],
                                        pip_size_by_symbol.get(symbol),
                                    )
                                    provider_message["digit_frequency"] = tracker.snapshot(symbol, time.time())
                            elif msg_type == "candles":
                                req_id = provider_message.get("req_id")
                                if isinstance(req_id, int):
                                    history_requests.pop(req_id, None)
                            elif msg_type == "tick":
                                tick = provider_message.get("tick")
                                if isinstance(tick, dict):
                                    symbol = tick.get("symbol") or tick.get("underlying_symbol")
                                    if not symbol:
                                        _log.debug("live_market_proxy tick missing 'symbol' field: %r", tick)
                                    if isinstance(symbol, str) and SYMBOL_PATTERN.fullmatch(symbol):
                                        tracker = frequency_by_symbol.setdefault(symbol, DigitFrequencyWindow())
                                        pip_size = tick.get("pip_size")
                                        if pip_size is None:
                                            pip_size = provider_message.get("pip_size")
                                        if isinstance(pip_size, int) and not isinstance(pip_size, bool) and 0 <= pip_size <= 12:
                                            pip_size_by_symbol[symbol] = pip_size
                                        tick_result = tracker.add_tick(
                                            tick.get("quote"),
                                            tick.get("epoch"),
                                            pip_size_by_symbol.get(symbol),
                                            None,
                                        )
                                        provider_message["digit_frequency"] = tracker.snapshot(symbol, time.time())
                                        active_strategy = (
                                            strategy_feed.symbol == symbol
                                            and strategy_feed.session is not None
                                            and strategy_feed.session.state.value not in {"idle", "stopped", "blocked"}
                                        )
                                        if active_strategy and tick_result not in {"accepted", "history_overlap"}:
                                            reason = (
                                                "Duplicate live tick detected."
                                                if tick_result == "duplicate"
                                                else "Invalid or out-of-order live tick detected."
                                            )
                                            tick_status = strategy_feed.reject_tick(reason)
                                            if tick_status:
                                                await websocket.send_json(tick_status)
                                        else:
                                            was_selected = bool(strategy_feed.session and strategy_feed.session.state.value == "selected")
                                            strategy_status = strategy_feed.on_frequency(provider_message["digit_frequency"])
                                            if strategy_status:
                                                await websocket.send_json(strategy_status)
                                            if was_selected:
                                                tick_status = strategy_feed.on_tick(symbol, tick, pip_size_by_symbol.get(symbol))
                                                if tick_status:
                                                    await websocket.send_json(tick_status)
                            raw = json.dumps(provider_message, separators=(",", ":"))
                        await websocket.send_text(raw)

                async def upstream_ping_loop() -> None:
                    while True:
                        await asyncio.sleep(30)
                        try:
                            await upstream.send(json.dumps({"ping": 1}, separators=(",", ":")))
                        except Exception:
                            return

                client_task = asyncio.create_task(client_to_upstream())
                upstream_task = asyncio.create_task(upstream_to_client())
                ping_task = asyncio.create_task(upstream_ping_loop())
                tasks = {client_task, upstream_task, ping_task}
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    if not task.cancelled():
                        exc = task.exception()
                        if exc and not isinstance(exc, (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)):
                            raise exc

                # If client_to_upstream finished first, the client disconnected — stop.
                if client_task in done:
                    return

                # Otherwise upstream closed; trigger reconnect loop.
                raise websockets.exceptions.ConnectionClosed(None, None)

        except WebSocketDisconnect:
            strategy_feed.disconnected()
            return
        except websockets.exceptions.ConnectionClosed:
            strategy_feed.disconnected()
            reconnect_attempt += 1
            if reconnect_attempt > _MAX_RECONNECT_ATTEMPTS:
                _log.warning("live_market_proxy: upstream closed after %d reconnect attempts; giving up.", _MAX_RECONNECT_ATTEMPTS)
                try:
                    await websocket.send_json({"error": {"code": "FEED_UNAVAILABLE", "message": "Market feed connection lost. Please reconnect."}})
                    await websocket.close(code=1011, reason="Market feed unavailable")
                except Exception:
                    pass
                return
            delay = min(2 ** (reconnect_attempt - 1), 30) + random.uniform(0, 1)
            _log.info("live_market_proxy: upstream closed, reconnecting in %.1fs (attempt %d/%d).", delay, reconnect_attempt, _MAX_RECONNECT_ATTEMPTS)
            await asyncio.sleep(delay)
        except Exception:
            strategy_feed.disconnected("Market feed failed unexpectedly.")
            try:
                await websocket.close(code=1011, reason="Market feed unavailable")
            except Exception:
                pass
            return
