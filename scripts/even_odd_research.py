"""Run Even/Odd research locally against the dedicated Supabase dataset.

Example:
    python -m scripts.even_odd_research --symbol R_50 --limit 5000

Set EVEN_ODD_SUPABASE_URL and EVEN_ODD_SUPABASE_SECRET_KEY in the local
environment first. Never commit these credentials or place them in source.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.even_odd.data_quality import inspect_historical_ticks
from app.even_odd.ml.pipeline import (
    run_research_experiment,
    run_walk_forward_evaluation,
)
from app.even_odd.storage import EvenOddStorageError, load_historical_ticks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run chronological Even/Odd ML research.")
    parser.add_argument("--symbol", required=True, help="Deriv market symbol, e.g. R_50.")
    parser.add_argument("--limit", type=int, default=5000, choices=range(1, 5001))
    parser.add_argument(
        "--windows",
        type=int,
        nargs="+",
        default=[50, 100, 250, 500, 1000],
    )
    parser.add_argument("--max-epochs", type=int, default=200)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--minimum-train-rows", type=int, default=500)
    parser.add_argument("--test-rows", type=int, default=250)
    parser.add_argument("--maximum-folds", type=int, default=5)
    return parser


async def _load(symbol: str, limit: int) -> dict:
    return await load_historical_ticks(symbol=symbol, limit=limit)


def main() -> int:
    args = _parser().parse_args()
    try:
        snapshot = asyncio.run(_load(args.symbol, args.limit))
        rows = snapshot["rows"]
        quality = inspect_historical_ticks(rows, expected_symbol=args.symbol)
        if not quality["quality_passed"]:
            print(json.dumps({
                "status": "blocked",
                "reason": "Historical dataset failed structural quality checks.",
                "snapshot_max_id": snapshot["snapshot_max_id"],
                "quality": quality,
            }, indent=2))
            return 2
        if quality["exact_market_observation_duplicates"]:
            print(
                "WARNING: repeated observation fingerprints detected; raw rows "
                "are retained and may overweight overlapping collection windows.",
                file=sys.stderr,
            )

        digits = [row["last_digit"] for row in rows]
        experiment = run_research_experiment(
            digits,
            windows=tuple(args.windows),
            max_epochs=args.max_epochs,
        )
        result = {
            "status": "research_complete",
            "symbol": args.symbol,
            "snapshot_max_id": snapshot["snapshot_max_id"],
            "limit": args.limit,
            "limit_reached": len(rows) == args.limit,
            "quality": quality,
            "experiment": experiment,
        }
        if args.walk_forward:
            result["walk_forward"] = run_walk_forward_evaluation(
                digits,
                windows=tuple(args.windows),
                minimum_train_rows=args.minimum_train_rows,
                test_rows=args.test_rows,
                maximum_folds=args.maximum_folds,
                max_epochs=args.max_epochs,
            )
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, EvenOddStorageError) as exc:
        print(json.dumps({
            "status": "error",
            "message": str(exc),
        }, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
