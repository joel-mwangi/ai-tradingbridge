"""Chronological expanding-window folds for Even/Odd model research."""
from __future__ import annotations

from typing import Sequence


def expanding_walk_forward_splits(
    rows: Sequence[dict],
    *,
    minimum_train_rows: int,
    test_rows: int,
    maximum_folds: int | None = None,
) -> list[dict[str, list[dict]]]:
    """Build non-overlapping future test blocks with expanding training history.

    Rows must already be ordered by target_index. No shuffle is performed.
    Each test block is strictly later than its corresponding training set.
    """
    if minimum_train_rows < 5:
        raise ValueError("minimum_train_rows must be at least 5.")
    if test_rows < 1:
        raise ValueError("test_rows must be positive.")
    if maximum_folds is not None and maximum_folds < 1:
        raise ValueError("maximum_folds must be positive when provided.")
    if len(rows) <= minimum_train_rows:
        raise ValueError("Not enough rows for a training set and future test block.")

    indices = [row.get("target_index") for row in rows]
    if any(type(index) is not int for index in indices):
        raise ValueError("Every row must contain an integer target_index.")
    if indices != sorted(indices) or len(set(indices)) != len(indices):
        raise ValueError("Rows must be uniquely ordered by target_index.")

    folds = []
    test_start = minimum_train_rows
    while test_start < len(rows):
        test_end = min(test_start + test_rows, len(rows))
        folds.append({
            "train": list(rows[:test_start]),
            "test": list(rows[test_start:test_end]),
        })
        if maximum_folds is not None and len(folds) >= maximum_folds:
            break
        test_start = test_end
    return folds
