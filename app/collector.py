import asyncio
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import websockets
from dotenv import load_dotenv

load_dotenv()

SYMBOL = os.getenv("DERIV_SYMBOL", "1HZ100V")
# Same public endpoint verified for the Cloudflare tick collector.
WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = DATA_DIR / f"{SYMBOL}_ticks.csv"

FIELDS = [
    "received_at_utc",
    "epoch",
    "symbol",
    "quote",
    "pip_size",
    "last_digit",
    "tick_id",
]


def prepare_csv():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
        with CSV_PATH.open("w", newline="", encoding="utf-8") as file:
            csv.DictWriter(file, fieldnames=FIELDS).writeheader()


def save_tick(tick):
    quote = tick.get("quote")
    epoch = tick.get("epoch")
    pip_size = tick.get("pip_size")

    if quote is None or epoch is None or pip_size is None:
        print("Skipping tick: missing quote, epoch, or pip_size.", flush=True)
        return False

    try:
        precision = int(pip_size)
        epoch = int(epoch)
        numeric_quote = float(quote)
    except (TypeError, ValueError, OverflowError):
        print("Skipping tick: invalid quote, epoch, or pip_size.", flush=True)
        return False

    if not 0 <= precision <= 10 or epoch <= 0 or numeric_quote <= 0:
        print("Skipping tick: quote, epoch, or precision out of range.", flush=True)
        return False

    formatted_quote = f"{numeric_quote:.{precision}f}"
    last_digit = int(formatted_quote.replace(".", "")[-1])
    row = {
        "received_at_utc": datetime.now(timezone.utc).isoformat(),
        "epoch": epoch,
        "symbol": tick.get("symbol", SYMBOL),
        "quote": formatted_quote,
        "pip_size": precision,
        "last_digit": last_digit,
        "tick_id": tick.get("id", ""),
    }

    with CSV_PATH.open("a", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=FIELDS).writerow(row)

    print(
        f"{row['received_at_utc']} | {row['symbol']} | "
        f"{formatted_quote} | digit={last_digit}",
        flush=True,
    )
    return True


async def collect():
    prepare_csv()
    while True:
        try:
            print(f"Connecting to Deriv for {SYMBOL}...", flush=True)
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({
                    "ticks": SYMBOL,
                    "subscribe": 1,
                    "req_id": 1,
                }))
                print("Subscription request sent.", flush=True)

                async for raw_message in ws:
                    message = json.loads(raw_message)
                    if message.get("error"):
                        error = message["error"]
                        raise RuntimeError(
                            f"Deriv API error: {error.get('message', error)}"
                        )
                    if message.get("msg_type") == "tick":
                        save_tick(message.get("tick", {}))
                    elif message.get("msg_type") == "subscription":
                        print("Tick subscription acknowledged.", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Collector error: {exc}", flush=True)
            print("Retrying in 5 seconds...", flush=True)
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(collect())
    except KeyboardInterrupt:
        print("\nCollector stopped. Saved data has been retained.")
