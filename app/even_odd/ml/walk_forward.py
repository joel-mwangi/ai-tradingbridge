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



def chronological_research_split(
    rows: Sequence[dict],
    *,
    train_fraction: float = 0.5,
    validation_fraction: float = 0.15,
    calibration_fraction: float = 0.15,
) -> dict[str, list[dict]]:
    """Reserve distinct chronological periods for fitting, tuning, calibration and test."""
    fractions = (train_fraction, validation_fraction, calibration_fraction)
    if any(not 0 < value < 1 for value in fractions):
        raise ValueError("Every split fraction must be between 0 and 1.")
    if sum(fractions) >= 1:
        raise ValueError("Split fractions must leave an untouched test set.")
    if len(rows) < 20:
        raise ValueError("At least 20 rows are required for chronological research splitting.")

    indices = [row.get("target_index") for row in rows]
    if any(type(index) is not int for index in indices):
        raise ValueError("Every row must contain an integer target_index.")
    if indices != sorted(indices) or len(set(indices)) != len(indices):
        raise ValueError("Rows must be uniquely ordered by target_index.")

    train_end = int(len(rows) * train_fraction)
    validation_end = train_end + int(len(rows) * validation_fraction)
    calibration_end = validation_end + int(len(rows) * calibration_fraction)
    if min(train_end, validation_end - train_end, calibration_end - validation_end,
           len(rows) - calibration_end) < 1:
        raise ValueError("Split fractions produce an empty research partition.")

    return {
        "train": list(rows[:train_end]),
        "validation": list(rows[train_end:validation_end]),
        "calibration": list(rows[validation_end:calibration_end]),
        "test": list(rows[calibration_end:]),
    }
