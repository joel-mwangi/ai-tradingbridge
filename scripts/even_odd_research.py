"""Run Even/Odd research locally and persist the run/model to dedicated Supabase.

Example:
    python -m scripts.even_odd_research --symbol R_50 --limit 5000 --walk-forward

Set EVEN_ODD_SUPABASE_URL and EVEN_ODD_SUPABASE_SECRET_KEY in the local
environment first. Never commit these credentials or place them in source.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

from app.even_odd.data_quality import inspect_historical_ticks
from app.even_odd.ml.pipeline import (
    run_research_experiment,
    run_walk_forward_evaluation,
)
from app.even_odd.storage import (
    EvenOddStorageError,
    complete_research_run,
    fail_research_run,
    load_historical_ticks,
    save_candidate_model,
    start_research_run,
)


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
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Explicitly allow repeated market-observation fingerprints in the research sample.",
    )
    parser.add_argument("--minimum-train-rows", type=int, default=500)
    parser.add_argument("--test-rows", type=int, default=250)
    parser.add_argument("--maximum-folds", type=int, default=5)
    return parser


async def _run(args: argparse.Namespace) -> int:
    research_run_id: str | None = None
    try:
        snapshot = await load_historical_ticks(
            symbol=args.symbol,
            limit=args.limit,
        )
        rows = snapshot["rows"]
        quality = inspect_historical_ticks(rows, expected_symbol=args.symbol)
        research_run_id = await start_research_run(
            symbol=args.symbol,
            snapshot_max_id=snapshot["snapshot_max_id"],
            dataset_rows=len(rows),
            quality_report=quality,
        )

        if not quality["quality_passed"]:
            await fail_research_run(research_run_id=research_run_id)
            print(json.dumps({
                "status": "blocked",
                "research_run_id": research_run_id,
                "reason": "Historical dataset failed structural quality checks.",
                "snapshot_max_id": snapshot["snapshot_max_id"],
                "quality": quality,
            }, indent=2))
            return 2

        if quality["exact_market_observation_duplicates"]:
            if not args.allow_duplicates:
                await fail_research_run(research_run_id=research_run_id)
                print(json.dumps({
                    "status": "blocked",
                    "research_run_id": research_run_id,
                    "reason": (
                        "Repeated observation fingerprints were detected. "
                        "Review the overlap or rerun with --allow-duplicates."
                    ),
                    "quality": quality,
                }, indent=2))
                return 2
            print(
                "WARNING: duplicates were explicitly allowed; repeated rows may "
                "overweight overlapping collection windows.",
                file=sys.stderr,
            )

        digits = [row["last_digit"] for row in rows]
        experiment = run_research_experiment(
            digits,
            windows=tuple(args.windows),
            max_epochs=args.max_epochs,
        )
        walk_forward = None
        if args.walk_forward:
            walk_forward = run_walk_forward_evaluation(
                digits,
                windows=tuple(args.windows),
                minimum_train_rows=args.minimum_train_rows,
                test_rows=args.test_rows,
                maximum_folds=args.maximum_folds,
                max_epochs=args.max_epochs,
            )

        model_version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        evaluation = {
            "test_metrics": experiment["test_metrics"],
            "constant_baseline_metrics": experiment["constant_baseline_metrics"],
            "rolling_frequency_baseline_metrics": experiment[
                "rolling_frequency_baseline_metrics"
            ],
            "promotion_status": experiment["promotion_status"],
            "walk_forward": walk_forward,
        }
        model_id = await save_candidate_model(
            symbol=args.symbol,
            model_version=model_version,
            model_artifact=experiment["model"],
            metrics=evaluation,
            snapshot_max_id=snapshot["snapshot_max_id"],
            partition_sizes=experiment["partitions"],
        )
        await complete_research_run(
            research_run_id=research_run_id,
            model_id=model_id,
            partition_sizes=experiment["partitions"],
            evaluation=evaluation,
        )

        print(json.dumps({
            "status": "research_complete",
            "research_run_id": research_run_id,
            "model_id": model_id,
            "model_version": model_version,
            "symbol": args.symbol,
            "snapshot_max_id": snapshot["snapshot_max_id"],
            "limit": args.limit,
            "limit_reached": len(rows) == args.limit,
            "quality": quality,
            "experiment": experiment,
            "walk_forward": walk_forward,
            "model_persisted_as": "candidate",
            "execution_authorized": False,
        }, indent=2, allow_nan=False))
        return 0
    except (ValueError, EvenOddStorageError) as exc:
        if research_run_id is not None:
            await fail_research_run(research_run_id=research_run_id)
        print(json.dumps({
            "status": "error",
            "research_run_id": research_run_id,
            "message": str(exc),
        }, indent=2))
        return 2


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
