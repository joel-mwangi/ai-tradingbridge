"""Build a bounded historical Even/Odd dataset from the dedicated Deriv feed.

The collector reuses the same isolated public WebSocket and Supabase storage
adapter as the API. It never connects to a trading account or places orders.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from datetime import datetime, timezone

from app.even_odd.collector import TickCollectionError, collect_ticks
from app.even_odd.data_quality import inspect_historical_ticks
from app.even_odd.storage import (
    EvenOddStorageError,
    fail_collection_run,
    finish_collection_run,
    load_historical_ticks,
    start_collection_run,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a bounded historical sample for Even/Odd research."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--target-rows", type=int, default=5000, choices=range(10, 5001))
    parser.add_argument("--batch-size", type=int, default=300, choices=range(10, 301))
    parser.add_argument("--timeout-seconds", type=int, default=80, choices=range(5, 81))
    parser.add_argument("--max-attempts", type=int, default=30, choices=range(1, 101))
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_]{2,32}", args.symbol):
        raise ValueError("Invalid Deriv market symbol.")

    snapshot = await load_historical_ticks(symbol=args.symbol, limit=5000)
    existing_rows = len(snapshot["rows"])
    remaining = max(0, args.target_rows - existing_rows)
    batches = 0
    collected_rows = 0

    while remaining > 0 and batches < args.max_attempts:
        requested = min(args.batch_size, remaining)
        started_at = datetime.now(timezone.utc)
        collection_id = await start_collection_run(
            symbol=args.symbol,
            requested_ticks=requested,
            started_at=started_at,
        )
        batches += 1

        try:
            ticks = await collect_ticks(
                symbol=args.symbol,
                tick_count=requested,
                timeout_seconds=args.timeout_seconds,
            )
            terminal_status = "completed"
            error_message = None
        except TickCollectionError as exc:
            ticks = exc.ticks
            if not ticks:
                await fail_collection_run(
                    collection_id=collection_id,
                    received_ticks=0,
                    error_message=str(exc),
                )
                raise RuntimeError(
                    f"Collection stopped after {collected_rows} new ticks: {exc}"
                ) from exc
            terminal_status = "partial"
            error_message = str(exc)

        await finish_collection_run(
            collection_id=collection_id,
            symbol=args.symbol,
            requested_ticks=requested,
            ticks=ticks,
            status=terminal_status,
            error_message=error_message,
        )
        collected_rows += len(ticks)
        remaining -= len(ticks)

    final_snapshot = await load_historical_ticks(
        symbol=args.symbol,
        limit=min(5000, args.target_rows),
    )
    quality = inspect_historical_ticks(
        final_snapshot["rows"],
        expected_symbol=args.symbol,
    )
    result = {
        "status": (
            "target_reached"
            if len(final_snapshot["rows"]) >= args.target_rows
            else "incomplete"
        ),
        "symbol": args.symbol,
        "target_rows": args.target_rows,
        "existing_rows_before_collection": existing_rows,
        "new_rows_collected": collected_rows,
        "rows_available_after_collection": len(final_snapshot["rows"]),
        "remaining_rows": max(0, args.target_rows - len(final_snapshot["rows"])),
        "collection_batches": batches,
        "max_attempts_reached": remaining > 0 and batches >= args.max_attempts,
        "snapshot_max_id": final_snapshot["snapshot_max_id"],
        "quality": quality,
        "order_execution_enabled": False,
    }
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "target_reached" and quality["quality_passed"] else 2


def main() -> int:
    args = _parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except (ValueError, RuntimeError, EvenOddStorageError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
