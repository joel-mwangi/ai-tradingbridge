"""Run adaptive Even/Odd conditional pattern research and persist the report.

This workflow does not train a next-tick classifier, create a predictive model
artifact, or authorize trade execution. It mines historical contexts and
estimates empirical outcome probabilities over data-adaptive future horizons.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.even_odd.data_quality import (
    canonicalize_overlapping_collection_ticks,
    inspect_historical_ticks,
)
from app.even_odd.ml.pattern_discovery import discover_patterns
from app.even_odd.storage import (
    EvenOddStorageError,
    complete_research_run,
    fail_research_run,
    load_historical_ticks,
    start_research_run,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover adaptive conditional patterns from live-captured Even/Odd observations."
    )
    parser.add_argument("--symbol", required=True, help="Deriv market symbol, e.g. R_50.")
    parser.add_argument("--limit", type=int, default=5000, choices=range(1, 5001))
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Explicitly allow repeated market-observation fingerprints in the research sample.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    research_run_id: str | None = None
    try:
        snapshot = await load_historical_ticks(
            symbol=args.symbol,
            limit=args.limit,
            capture_mode="all_live",
        )
        raw_rows = snapshot["rows"]
        if not raw_rows:
            print(json.dumps({
                "status": "blocked",
                "reason": (
                    "No live-captured observations are available yet. Start browser-managed "
                    "live collection and wait for data to accumulate."
                ),
                "historical_imports_enabled": False,
                "model_training_performed": False,
                "order_execution_enabled": False,
            }, indent=2))
            return 2

        canonical = canonicalize_overlapping_collection_ticks(raw_rows)
        rows = canonical["rows"]
        quality = inspect_historical_ticks(rows, expected_symbol=args.symbol)
        quality["raw_observation_count"] = canonical["raw_rows"]
        quality["overlap_duplicates_excluded"] = canonical["overlap_duplicates_excluded"]
        quality["canonical_sample_size"] = canonical["canonical_rows"]
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
                "reason": "Live-captured dataset failed structural quality checks.",
                "snapshot_max_id": snapshot["snapshot_max_id"],
                "quality": quality,
                "model_training_performed": False,
            }, indent=2))
            return 2

        if quality["exact_market_observation_duplicates"]:
            if not args.allow_duplicates:
                await fail_research_run(research_run_id=research_run_id)
                print(json.dumps({
                    "status": "blocked",
                    "research_run_id": research_run_id,
                    "reason": (
                        "Repeated observation fingerprints were detected in the research sample. "
                        "Review overlapping collections or rerun with --allow-duplicates."
                    ),
                    "quality": quality,
                    "model_training_performed": False,
                }, indent=2))
                return 2
            print(
                "WARNING: duplicates were explicitly allowed; repeated rows may "
                "overweight overlapping collection windows.",
                file=sys.stderr,
            )

        digits = [row["last_digit"] for row in rows]
        pattern_discovery = discover_patterns(digits)
        evaluation = {
            "research_objective": "adaptive_conditional_pattern_discovery",
            "pattern_discovery": pattern_discovery,
            "model_training": {
                "performed": False,
                "reason": "The research objective is conditional pattern discovery and multi-horizon occurrence probabilities, not next-tick classification.",
            },
            "promotion_status": {
                "validated": False,
                "execution_authorized": False,
                "reason": (
                    "Pattern probabilities are exploratory historical conditional frequencies. "
                    "They require independent chronological validation, multiple-testing controls, "
                    "payout-aware replay and approval before any trading use."
                ),
            },
        }
        await complete_research_run(
            research_run_id=research_run_id,
            model_id=None,
            partition_sizes={"pattern_discovery_observations": len(digits)},
            evaluation=evaluation,
        )

        print(json.dumps({
            "status": "research_complete",
            "research_run_id": research_run_id,
            "model_id": None,
            "symbol": args.symbol,
            "snapshot_max_id": snapshot["snapshot_max_id"],
            "limit": args.limit,
            "limit_reached": len(rows) == args.limit,
            "quality": quality,
            "pattern_discovery": pattern_discovery,
            "model_training_performed": False,
            "model_training_objective": "conditional_pattern_occurrence_probabilities",
            "order_execution_enabled": False,
            "historical_imports_enabled": False,
        }, indent=2, allow_nan=False))
        return 0
    except (ValueError, EvenOddStorageError) as exc:
        if research_run_id is not None:
            await fail_research_run(research_run_id=research_run_id)
        print(json.dumps({
            "status": "error",
            "research_run_id": research_run_id,
            "message": str(exc),
            "model_training_performed": False,
        }, indent=2))
        return 2


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
