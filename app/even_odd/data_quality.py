"""Quality diagnostics for persisted Even/Odd research observations.

The audit reports anomalies without silently dropping or rewriting source rows.
It intentionally does not infer statistical predictability from frequency data.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence


REQUIRED_FIELDS = (
    "id",
    "collection_id",
    "sequence_no",
    "market_symbol",
    "tick_epoch",
    "quote",
    "pip_size",
    "last_digit",
    "parity",
)


def inspect_historical_ticks(
    rows: Sequence[dict[str, Any]],
    *,
    expected_symbol: str | None = None,
) -> dict[str, Any]:
    """Return structural, quote-integrity, ordering and duplicate diagnostics."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError("rows must be a list or tuple of tick records.")

    missing_field_rows = 0
    invalid_rows = 0
    symbol_mismatches = 0
    parity_mismatches = 0
    quote_digit_mismatches = 0
    ordering_violations = 0
    fingerprints: Counter[tuple] = Counter()
    collection_ids: set[str] = set()
    valid_epochs: list[int] = []
    previous_order: tuple[int, int] | None = None
    previous_sequence_by_collection: dict[str, int] = {}
    seen_sequences: set[tuple[str, int]] = set()
    duplicate_sequence_numbers = 0
    sequence_order_violations = 0

    for row in rows:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue

        missing = [field for field in REQUIRED_FIELDS if field not in row]
        if missing:
            missing_field_rows += 1
            invalid_rows += 1
            continue

        try:
            if any(
                type(row[field]) is not int
                for field in ("id", "sequence_no", "tick_epoch", "pip_size", "last_digit")
            ):
                raise ValueError
            row_id = row["id"]
            sequence_no = row["sequence_no"]
            epoch = row["tick_epoch"]
            pip_size = row["pip_size"]
            digit = row["last_digit"]
            symbol = row["market_symbol"]
            quote = Decimal(str(row["quote"]))
            parity = row["parity"]
            collection_id = str(row["collection_id"])
            if row_id <= 0 or sequence_no <= 0 or epoch <= 0 or not 0 <= pip_size <= 10:
                raise ValueError
            if not quote.is_finite() or quote <= 0 or not 0 <= digit <= 9:
                raise ValueError
            if not isinstance(symbol, str) or not symbol:
                raise ValueError
            if parity not in {"even", "odd"}:
                raise ValueError
        except (ValueError, TypeError, InvalidOperation, OverflowError):
            invalid_rows += 1
            continue

        if expected_symbol is not None and symbol != expected_symbol:
            symbol_mismatches += 1
        expected_parity = "even" if digit % 2 == 0 else "odd"
        if parity != expected_parity:
            parity_mismatches += 1

        formatted_quote = format(quote, f".{pip_size}f")
        quote_digit = int(formatted_quote.replace(".", "")[-1])
        if quote_digit != digit:
            quote_digit_mismatches += 1

        order_key = (epoch, row_id)
        if previous_order is not None and order_key < previous_order:
            ordering_violations += 1
        previous_order = order_key

        sequence_key = (collection_id, sequence_no)
        if sequence_key in seen_sequences:
            duplicate_sequence_numbers += 1
        seen_sequences.add(sequence_key)
        previous_sequence = previous_sequence_by_collection.get(collection_id)
        if previous_sequence is not None and sequence_no <= previous_sequence:
            sequence_order_violations += 1
        previous_sequence_by_collection[collection_id] = sequence_no

        fingerprints[(symbol, epoch, formatted_quote, pip_size)] += 1
        collection_ids.add(collection_id)
        valid_epochs.append(epoch)

    duplicate_rows = sum(count - 1 for count in fingerprints.values() if count > 1)
    valid_rows = len(rows) - invalid_rows
    return {
        "sample_size": len(rows),
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "missing_field_rows": missing_field_rows,
        "symbol_mismatches": symbol_mismatches,
        "parity_mismatches": parity_mismatches,
        "quote_digit_mismatches": quote_digit_mismatches,
        "chronological_order_violations": ordering_violations,
        "duplicate_sequence_numbers": duplicate_sequence_numbers,
        "sequence_order_violations": sequence_order_violations,
        "exact_market_observation_duplicates": duplicate_rows,
        "collection_count": len(collection_ids),
        "first_tick_epoch": min(valid_epochs) if valid_epochs else None,
        "last_tick_epoch": max(valid_epochs) if valid_epochs else None,
        "quality_passed": (
            invalid_rows == 0
            and symbol_mismatches == 0
            and parity_mismatches == 0
            and quote_digit_mismatches == 0
            and ordering_violations == 0
            and duplicate_sequence_numbers == 0
            and sequence_order_violations == 0
        ),
        "duplicate_policy": (
            "Duplicates are reported but retained. Identical quote and epoch "
            "values can represent repeated genuine ticks or overlapping collection "
            "windows; automatic removal would risk discarding valid observations."
        ),
        "interpretation": (
            "Structural quality checks only. Passing this audit does not establish "
            "independence, stationarity, statistical significance, or predictive edge."
        ),
    }
