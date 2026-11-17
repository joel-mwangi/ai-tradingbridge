"""Tests for app.strategy.market_scanner."""
from __future__ import annotations

import pytest

from app.strategy.market_scanner import (
    get_best_signal,
    scan_all_contracts,
    scan_maziwa_markets,
    score_contract,
)
from app.strategy.digit_strategy import ContractType


def _uniform_percentages(value: float = 10.0) -> dict[int, float]:
    """Return a percentage map where every digit has the same value."""
    return {d: value for d in range(10)}


def _balanced_base() -> dict[int, float]:
    """Base map close to uniform (9.9%) with room to elevate individual digits."""
    return {d: 9.9 for d in range(10)}


# ---------------------------------------------------------------------------
# test 1: DIGITOVER – digit in losing zone ≥12% → score > 0 and threshold met
# ---------------------------------------------------------------------------
def test_score_contract_over_returns_high_when_threshold_met() -> None:
    pcts = _balanced_base()
    # Elevate digit 2 (which is in the losing zone for OVER barrier=4: digits 0–4)
    pcts[2] = 13.0
    # Adjust digit 0 slightly down so total stays near 100%
    pcts[0] = 9.0
    result = score_contract(pcts, "1HZ100V", "DIGITOVER", 4)
    assert result.threshold_met is True
    assert result.score > 0


# ---------------------------------------------------------------------------
# test 2: DIGITEVEN – an even digit ≥12% → score > 0
# ---------------------------------------------------------------------------
def test_score_contract_even_returns_score_when_even_digit_above_threshold() -> None:
    pcts = _balanced_base()
    pcts[4] = 14.0   # even digit 4 at 14%
    pcts[0] = 8.9    # adjust to keep total near 100%
    result = score_contract(pcts, "R_100", "DIGITEVEN", None)
    assert result.score > 0
    assert result.threshold_met is True
    assert result.qualifying_digit == 4


# ---------------------------------------------------------------------------
# test 3: DIGITDIFF – target digit depleted (≤7%) → score > 0
# ---------------------------------------------------------------------------
def test_score_contract_differs_returns_score_when_digit_depleted() -> None:
    pcts = _balanced_base()
    pcts[5] = 5.0    # digit 5 is depleted
    pcts[0] = 14.8   # compensate
    result = score_contract(pcts, "1HZ75V", "DIGITDIFF", 5)
    assert result.score > 0
    assert result.threshold_met is True
    assert result.qualifying_digit == 5


# ---------------------------------------------------------------------------
# test 4: scan_all_contracts returns sorted results (descending by score)
# ---------------------------------------------------------------------------
def test_scan_all_contracts_returns_sorted_results() -> None:
    pcts = _balanced_base()
    # Elevate digit 2 to trigger DIGITOVER barrier=4
    pcts[2] = 13.0
    pcts[0] = 9.0
    results = scan_all_contracts({"1HZ100V": pcts})
    assert len(results) > 0
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True), "Results must be sorted by score descending."


# ---------------------------------------------------------------------------
# test 5: get_best_signal returns None when nothing exceeds min_score
# ---------------------------------------------------------------------------
def test_get_best_signal_returns_none_when_no_signal_above_min_score() -> None:
    # Uniform 10% → no digit above 12% threshold → no qualifying contracts
    pcts = _uniform_percentages(10.0)
    result = get_best_signal({"R_75": pcts}, min_score=80.0)
    assert result is None


# ---------------------------------------------------------------------------
# test 6: get_best_signal returns a result when a HIGH signal exists
# ---------------------------------------------------------------------------
def test_get_best_signal_returns_top_result_when_above_threshold() -> None:
    pcts = _balanced_base()
    # Elevate digit 3 strongly to push it to GREEN (highest) in its zone,
    # so DIGITOVER barrier=4 fires with HIGH confidence.
    # Digits 0-4 are the "losing zone"; make digit 3 clearly the highest.
    pcts[3] = 15.0   # well above 12%, will be GREEN
    pcts[5] = 14.5   # second highest overall → BLUE
    pcts[6] = 13.5
    pcts[7] = 12.5
    pcts[8] = 11.5
    pcts[9] = 10.5
    pcts[0] = 8.0
    pcts[1] = 8.0
    pcts[2] = 8.0
    pcts[4] = 8.5
    result = get_best_signal({"1HZ100V": pcts}, min_score=80.0)
    assert result is not None
    assert result.score >= 80.0


def _maziwa_percentages(target: ContractType, power: float = 55.0) -> dict[int, float]:
    target_digits = {0, 2, 4, 6, 8} if target is ContractType.EVEN else {1, 3, 5, 7, 9}
    opposing_digits = set(range(10)) - target_digits
    percentages = {digit: 9.0 for digit in range(10)}
    target_values = [12.1, (power - 12.1) / 4, (power - 12.1) / 4, (power - 12.1) / 4, (power - 12.1) / 4]
    for digit, value in zip(sorted(target_digits), target_values):
        percentages[digit] = value
    percentages[min(opposing_digits)] = 8.0
    percentages[max(opposing_digits)] = 8.5
    return percentages


def test_maziwa_scanner_does_not_require_confirmation_streak() -> None:
    current = _maziwa_percentages(ContractType.EVEN)
    previous = {digit: value - 0.1 for digit, value in current.items()}
    results = scan_maziwa_markets({
        "1HZ100V": (current, previous, [2, 4, 6, 8], 1000),
    })
    assert results
    assert results[0].status == "qualified_waiting_for_confirmation"
    assert results[0].score > 0


def test_maziwa_scanner_does_not_require_rising_power_history() -> None:
    current = _maziwa_percentages(ContractType.ODD)
    results = scan_maziwa_markets({
        "R_75": (current, None, [1, 3, 5, 7], 1000),
    })
    assert len(results) == 1
    assert results[0].status == "qualified_waiting_for_confirmation"
    assert results[0].sample_size == 1000
