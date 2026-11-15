"""Pure scanner engine: scores every viable contract across all subscribed symbols.

No FastAPI, WebSocket, or I/O imports. This module is stateless and side-effect-free.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from app.strategy.digit_strategy import (
    ContractType,
    evaluate_color_positions,
    evaluate_threshold_signal,
    Group,
)

# Threshold for EVEN/ODD/OVER/UNDER qualifying digit
_THRESHOLD_PCT = 12.0
# Higher bar for MATCHES (single digit must be strongly favoured)
_MATCHES_THRESHOLD_PCT = 14.0
# For DIFFERS: a digit that rarely appeared → "differs" bet looks attractive
_DIFFERS_DEPLETED_MAX_PCT = 7.0


@dataclass
class ContractSpec:
    """Describes a single tradeable digit contract."""

    symbol: str
    contract_type: str  # "DIGITOVER" | "DIGITUNDER" | "DIGITEVEN" | "DIGITODD" | "DIGITMATCH" | "DIGITDIFF"
    barrier: int | None  # None for EVEN / ODD
    direction: str  # "over" | "under" | "even" | "odd" | "matches" | "differs"


@dataclass
class ScanResult:
    """Score and metadata for one (symbol, contract) evaluation."""

    spec: ContractSpec
    score: float          # 0.0–100.0
    qualifying_digit: int | None
    qualifying_pct: float | None
    confidence: str       # "HIGH" | "MEDIUM" | "NONE"
    threshold_met: bool
    color_positions_pass: bool
    reason: str
    scanned_at: float     # epoch timestamp


def _valid_pct_map(percentages: Mapping[int, float]) -> bool:
    """Accept only mappings where all ten digits map to finite floats in [0, 100]."""
    if not isinstance(percentages, Mapping):
        return False
    import math as _math
    for d in range(10):
        if d not in percentages:
            return False
        v = percentages[d]
        if isinstance(v, bool):
            return False
        try:
            fv = float(v)
        except (TypeError, ValueError, OverflowError):
            return False
        if not _math.isfinite(fv) or fv < 0.0 or fv > 100.0:
            return False
    return True


def score_contract(
    percentages: Mapping[int, float],
    symbol: str,
    contract_type: str,
    barrier: int | None,
) -> ScanResult:
    """Score one (market, contract_type, barrier) combination.

    Scoring:
      +60  threshold met (qualifying digit ≥12% in losing zone)
      +20  colour positions pass (OVER/UNDER only)
      +20  confidence HIGH
      +10  confidence MEDIUM
      0    threshold not met → score = 0
    """
    now = time.time()

    # Determine direction string for ContractSpec
    direction_map = {
        "DIGITOVER": "over",
        "DIGITUNDER": "under",
        "DIGITEVEN": "even",
        "DIGITODD": "odd",
        "DIGITMATCH": "matches",
        "DIGITDIFF": "differs",
    }
    direction = direction_map.get(contract_type, "over")
    spec = ContractSpec(
        symbol=symbol,
        contract_type=contract_type,
        barrier=barrier,
        direction=direction,
    )

    if not _valid_pct_map(percentages):
        return ScanResult(
            spec=spec,
            score=0.0,
            qualifying_digit=None,
            qualifying_pct=None,
            confidence="NONE",
            threshold_met=False,
            color_positions_pass=False,
            reason="Invalid percentage map.",
            scanned_at=now,
        )

    threshold_met = False
    qualifying_digit: int | None = None
    qualifying_pct: float | None = None
    confidence = "NONE"
    color_pass = False
    reason_parts: list[str] = []

    if contract_type == "DIGITOVER":
        if barrier is None:
            return ScanResult(spec, 0.0, None, None, "NONE", False, False,
                              "OVER requires a barrier.", now)
        sig = evaluate_threshold_signal(percentages, ContractType.OVER, barrier)
        threshold_met = sig.triggered
        qualifying_digit = sig.qualifying_digit
        qualifying_pct = sig.qualifying_pct
        confidence = sig.confidence
        reason_parts.append(sig.reason)
        if threshold_met:
            color_ok, color_reason = evaluate_color_positions(percentages, Group.OVER, barrier)
            color_pass = color_ok
            reason_parts.append(color_reason)

    elif contract_type == "DIGITUNDER":
        if barrier is None:
            return ScanResult(spec, 0.0, None, None, "NONE", False, False,
                              "UNDER requires a barrier.", now)
        sig = evaluate_threshold_signal(percentages, ContractType.UNDER, barrier)
        threshold_met = sig.triggered
        qualifying_digit = sig.qualifying_digit
        qualifying_pct = sig.qualifying_pct
        confidence = sig.confidence
        reason_parts.append(sig.reason)
        if threshold_met:
            color_ok, color_reason = evaluate_color_positions(percentages, Group.UNDER, barrier)
            color_pass = color_ok
            reason_parts.append(color_reason)

    elif contract_type == "DIGITEVEN":
        # Delegate entirely to evaluate_threshold_signal so qualifying_digit,
        # qualifying_pct, confidence, and reason all describe the same digit.
        sig = evaluate_threshold_signal(percentages, ContractType.EVEN)
        threshold_met = sig.triggered
        qualifying_digit = sig.qualifying_digit
        qualifying_pct = sig.qualifying_pct
        confidence = sig.confidence
        reason_parts.append(sig.reason)

    elif contract_type == "DIGITODD":
        sig = evaluate_threshold_signal(percentages, ContractType.ODD)
        threshold_met = sig.triggered
        qualifying_digit = sig.qualifying_digit
        qualifying_pct = sig.qualifying_pct
        confidence = sig.confidence
        reason_parts.append(sig.reason)

    elif contract_type == "DIGITMATCH":
        if barrier is None:
            return ScanResult(spec, 0.0, None, None, "NONE", False, False,
                              "MATCH requires a barrier.", now)
        pct = float(percentages[barrier])
        if pct >= _MATCHES_THRESHOLD_PCT:
            threshold_met = True
            qualifying_digit = barrier
            qualifying_pct = pct
            # For MATCHES, confidence: HIGH if pct >= 16%, MEDIUM otherwise
            if pct >= 16.0:
                confidence = "HIGH"
                reason_parts.append(f"Digit {barrier} at {pct:.1f}% meets MATCH threshold (HIGH).")
            else:
                confidence = "MEDIUM"
                reason_parts.append(f"Digit {barrier} at {pct:.1f}% meets MATCH threshold (MEDIUM).")
        else:
            reason_parts.append(f"Digit {barrier} at {float(percentages[barrier]):.1f}% is below {_MATCHES_THRESHOLD_PCT}% MATCH threshold.")

    elif contract_type == "DIGITDIFF":
        if barrier is None:
            return ScanResult(spec, 0.0, None, None, "NONE", False, False,
                              "DIFF requires a barrier.", now)
        pct = float(percentages[barrier])
        if pct <= _DIFFERS_DEPLETED_MAX_PCT:
            threshold_met = True
            qualifying_digit = barrier
            qualifying_pct = pct
            # Confidence: the lower the pct, the higher the confidence
            if pct <= 4.0:
                confidence = "HIGH"
                reason_parts.append(f"Digit {barrier} at {pct:.1f}% is depleted (HIGH confidence for DIFFERS).")
            else:
                confidence = "MEDIUM"
                reason_parts.append(f"Digit {barrier} at {pct:.1f}% is depleted (MEDIUM confidence for DIFFERS).")
        else:
            reason_parts.append(f"Digit {barrier} at {pct:.1f}% is not depleted enough for DIFFERS.")

    else:
        return ScanResult(spec, 0.0, None, None, "NONE", False, False,
                          f"Unknown contract_type: {contract_type}", now)

    if not threshold_met:
        return ScanResult(
            spec=spec,
            score=0.0,
            qualifying_digit=qualifying_digit,
            qualifying_pct=qualifying_pct,
            confidence="NONE",
            threshold_met=False,
            color_positions_pass=False,
            reason=" ".join(reason_parts),
            scanned_at=now,
        )

    # Build score
    score = 60.0
    if color_pass:
        score += 20.0
    if confidence == "HIGH":
        score += 20.0
    elif confidence == "MEDIUM":
        score += 10.0

    return ScanResult(
        spec=spec,
        score=score,
        qualifying_digit=qualifying_digit,
        qualifying_pct=qualifying_pct,
        confidence=confidence,
        threshold_met=True,
        color_positions_pass=color_pass,
        reason=" ".join(reason_parts),
        scanned_at=now,
    )


def scan_all_contracts(
    percentages_by_symbol: dict[str, dict[int, float]],
) -> list[ScanResult]:
    """Evaluate all viable contracts across all symbols; return qualifying results sorted by score.

    Viable contract ranges:
      OVER:    barriers 1–8   (OVER 0 and OVER 9 are invalid)
      UNDER:   barriers 1–8   (UNDER 0 and UNDER 9 are invalid)
      EVEN:    no barrier
      ODD:     no barrier
      MATCHES: barriers 0–9
      DIFFERS: barriers 0–9
    """
    results: list[ScanResult] = []

    for symbol, percentages in percentages_by_symbol.items():
        if not _valid_pct_map(percentages):
            continue

        # OVER barriers 1–8
        for barrier in range(1, 9):
            r = score_contract(percentages, symbol, "DIGITOVER", barrier)
            if r.score > 0:
                results.append(r)

        # UNDER barriers 1–8
        for barrier in range(1, 9):
            r = score_contract(percentages, symbol, "DIGITUNDER", barrier)
            if r.score > 0:
                results.append(r)

        # EVEN (no barrier)
        r = score_contract(percentages, symbol, "DIGITEVEN", None)
        if r.score > 0:
            results.append(r)

        # ODD (no barrier)
        r = score_contract(percentages, symbol, "DIGITODD", None)
        if r.score > 0:
            results.append(r)

        # MATCHES barriers 0–9
        for barrier in range(10):
            r = score_contract(percentages, symbol, "DIGITMATCH", barrier)
            if r.score > 0:
                results.append(r)

        # DIFFERS barriers 0–9
        for barrier in range(10):
            r = score_contract(percentages, symbol, "DIGITDIFF", barrier)
            if r.score > 0:
                results.append(r)

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def get_best_signal(
    percentages_by_symbol: dict[str, dict[int, float]],
    min_score: float = 80.0,
) -> ScanResult | None:
    """Return the top-ranked result if its score meets min_score, else None."""
    results = scan_all_contracts(percentages_by_symbol)
    if results and results[0].score >= min_score:
        return results[0]
    return None
