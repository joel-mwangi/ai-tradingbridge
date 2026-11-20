"""Continuous live-only Deriv tick collector for Even/Odd Lab.

Run as an always-on background worker, not as a Vercel request handler.
It never calls ticks_history and persists only ticks received after subscription.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from datetime import datetime, timezone

import websockets

from app.even_odd.collector import DERIV_PUBLIC_WS_URL, normalize_tick
from app.even_odd.storage import (
    EvenOddStorageError,
    persist_live_tick_batch,
    update_live_collector_heartbeat,
)

BATCH_SIZE = 100
FLUSH_SECONDS = 5
HEARTBEAT_SECONDS = 15
MAX_BACKOFF_SECONDS = 60


def configured_symbols() -> list[str]:
    raw = os.getenv("EVEN_ODD_LIVE_SYMBOLS", "R_50")
    symbols = list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not symbols:
        raise ValueError("Set EVEN_ODD_LIVE_SYMBOLS to at least one Deriv symbol.")
    return symbols


async def run_worker() -> None:
    symbols = configured_symbols()
    worker_id = os.getenv("EVEN_ODD_WORKER_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc)
    buffers: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    totals = {symbol: 0 for symbol in symbols}
    last_tick_at: dict[str, datetime | None] = {symbol: None for symbol in symbols}
    last_heartbeat = 0.0
    backoff = 2
    request_symbols = {req_id: symbol for req_id, symbol in enumerate(symbols, start=5100)}

    async def heartbeat(status: str, error: str | None = None) -> None:
        for symbol in symbols:
            await update_live_collector_heartbeat(
                symbol=symbol,
                worker_id=worker_id,
                status=status,
                started_at=started_at,
                last_tick_at=last_tick_at[symbol],
                ticks_received=totals[symbol],
                last_error=error,
            )

    async def flush(symbol: str) -> None:
        batch = buffers[symbol]
        if not batch:
            return
        # Keep a copy until the database confirms persistence. A failed insert
        # must not silently discard observations from the in-memory queue.
        await persist_live_tick_batch(
            symbol=symbol,
            ticks=list(batch),
            worker_id=worker_id,
        )
        totals[symbol] += len(batch)
        buffers[symbol] = []

    while True:
        try:
            await heartbeat("connecting")
            async with websockets.connect(
                DERIV_PUBLIC_WS_URL,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
                close_timeout=5,
                max_size=1_000_000,
            ) as websocket:
                for req_id, symbol in enumerate(symbols, start=5100):
                    await websocket.send(json.dumps({
                        "ticks": symbol,
                        "subscribe": 1,
                        "req_id": req_id,
                    }))
                await heartbeat("connected")
                backoff = 2
                while True:
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=FLUSH_SECONDS)
                    except asyncio.TimeoutError:
                        for symbol in symbols:
                            await flush(symbol)
                        await heartbeat("connected")
                        continue

                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        continue
                    if message.get("error"):
                        error = message["error"]
                        detail = error.get("message", "Deriv rejected a live tick subscription.") if isinstance(error, dict) else "Deriv rejected a live tick subscription."
                        raise RuntimeError(str(detail))

                    if message.get("msg_type") != "tick":
                        continue
                    payload = message.get("tick")
                    if not isinstance(payload, dict):
                        continue
                    symbol = payload.get("symbol")
                    if symbol not in buffers:
                        symbol = request_symbols.get(message.get("req_id"))
                    if symbol not in buffers:
                        continue

                    try:
                        normalized = normalize_tick({**payload, "symbol": symbol}, symbol)
                    except ValueError:
                        continue
                    # Deriv's optional tick.id is kept separately from the
                    # per-subscription stream identifier. Neither is assumed
                    # globally unique by this collector.
                    normalized["tick_id"] = normalized.pop("stream_id", None)
                    subscription = message.get("subscription")
                    normalized["stream_id"] = (
                        str(subscription.get("id"))
                        if isinstance(subscription, dict) and subscription.get("id") is not None
                        else None
                    )
                    buffers[symbol].append(normalized)
                    last_tick_at[symbol] = datetime.now(timezone.utc)

                    if len(buffers[symbol]) >= BATCH_SIZE:
                        await flush(symbol)

                    now = asyncio.get_running_loop().time()
                    if now - last_heartbeat >= HEARTBEAT_SECONDS:
                        await heartbeat("connected")
                        last_heartbeat = now

        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError, websockets.WebSocketException, EvenOddStorageError) as exc:
            try:
                for symbol in symbols:
                    await flush(symbol)
            except EvenOddStorageError:
                pass
            try:
                await heartbeat("reconnecting", str(exc))
            except EvenOddStorageError:
                print("Live collector heartbeat could not reach the dedicated database.")
            print(f"Live collector connection error: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        print("Even/Odd live collector stopped.")


if __name__ == "__main__":
    main()
