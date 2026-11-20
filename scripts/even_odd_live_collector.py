"""Compatibility entrypoint for the single manual collection worker.

The old continuous market collector is retired. This process idles without
subscribing to Deriv until a user-created collection job is available.
"""
import asyncio
import os
import re

from scripts.even_odd_manual_collection_worker import run_worker


def configured_symbols() -> list[str]:
    """Retain the legacy configuration helper for compatibility and tests."""
    raw = os.getenv("EVEN_ODD_LIVE_SYMBOLS", "R_50")
    symbols = list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not symbols:
        raise ValueError("Set EVEN_ODD_LIVE_SYMBOLS to at least one Deriv symbol.")
    if any(not re.fullmatch(r"[A-Za-z0-9_]{2,32}", symbol) for symbol in symbols):
        raise ValueError("EVEN_ODD_LIVE_SYMBOLS contains an invalid Deriv symbol.")
    return symbols


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        print("Even/Odd manual collection worker stopped.")


if __name__ == "__main__":
    main()
