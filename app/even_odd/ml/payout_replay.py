"""Chronological payout-aware replay and out-of-sample opportunity persistence.

This evaluator uses only contemporaneous stored quotes and fold-local forecasts.
It never submits Deriv orders or substitutes today's payout quote for history.
"""
from __future__ import annotations

from bisect import bisect_right
from math import isfinite, sqrt
from typing import Sequence


def _wilson_lower_bound(successes: int, trials: int, *, z: float = 1.645) -> float:
    """One-sided 90% Wilson lower confidence bound for a Bernoulli rate."""
    if trials <= 0:
        return 0.0
    rate = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    center = rate + z2 / (2 * trials)
    margin = z * sqrt((rate * (1 - rate) + z2 / (4 * trials)) / trials)
    return max(0.0, (center - margin) / denominator)


def _estimate_sequence_persistence(
    records: Sequence[dict],
    *,
    maximum_horizon: int = 6,
    minimum_trials: int = 200,
    minimum_folds: int = 5,
) -> dict[str, dict]:
    """Estimate same-side opportunity survival using only consecutive OOS rows."""
    result = {}
    for horizon in range(1, maximum_horizon + 1):
        successes = 0
        trials = 0
        folds: set[int] = set()
        for index, record in enumerate(records):
            if record.get("qualifies") is not True:
                continue
            fold = record.get("fold")
            following = records[index + 1:index + horizon + 1]
            if len(following) != horizon:
                continue
            if any(item.get("fold") != fold for item in following):
                continue
            trials += 1
            folds.add(int(fold))
            if all(
                item.get("qualifies") is True
                and item.get("side") == record.get("side")
                for item in following
            ):
                successes += 1
        lower = _wilson_lower_bound(successes, trials)
        validated = trials >= minimum_trials and len(folds) >= minimum_folds
        result[str(horizon)] = {
            "horizon": horizon,
            "successes": successes,
            "trials": trials,
            "fold_count": len(folds),
            "persistence_rate": successes / trials if trials else None,
            "lower_confidence_bound_90": lower if validated else None,
            "validated": validated,
            "method": "same_side_consecutive_oos_opportunity_survival_wilson_90",
            "reason": (
                "Minimum OOS trial and fold requirements met."
                if validated
                else "Insufficient same-side OOS opportunities or fold coverage."
            ),
        }
    return result


def replay_payout_aware_strategy(
    predictions: Sequence[dict],
    quote_snapshots: Sequence[dict],
    *,
    stake: float,
    maximum_quote_age_seconds: int = 10,
    minimum_edge: float = 0.05,
    minimum_expected_value: float = 0.10,
    minimum_trades_for_evidence: int = 200,
) -> dict:
    """Replay OOS predictions against the latest prior quote for the same contract.

    Predictions require a fold-local calibrated probability interval. The replay
    selects a side only when its conservative edge and EV clear policy thresholds.
    Missing, future, stale or ambiguous quotes never create a simulated trade.
    """
    if isinstance(stake, bool) or not isinstance(stake, (int, float)) or not isfinite(stake) or stake <= 0:
        raise ValueError("stake must be finite and positive.")
    if type(maximum_quote_age_seconds) is not int or maximum_quote_age_seconds < 0:
        raise ValueError("maximum_quote_age_seconds must be a non-negative integer.")
    if not 0 < minimum_edge < 1 or not isfinite(minimum_edge):
        raise ValueError("minimum_edge must be finite and between 0 and 1.")
    if not isfinite(minimum_expected_value) or minimum_expected_value < 0:
        raise ValueError("minimum_expected_value must be finite and non-negative.")
    if type(minimum_trades_for_evidence) is not int or minimum_trades_for_evidence < 1:
        raise ValueError("minimum_trades_for_evidence must be positive.")

    quotes = sorted(quote_snapshots, key=lambda item: int(item["spot_time"]))
    quote_epochs = [int(item["spot_time"]) for item in quotes]
    if len(set(quote_epochs)) != len(quote_epochs):
        raise ValueError("Quote snapshots must have unique spot_time values.")
    for quote in quotes:
        for field in ("even_ask_price", "even_payout", "odd_ask_price", "odd_payout"):
            value = float(quote[field])
            if not isfinite(value) or value <= 0:
                raise ValueError(f"Quote {field} must be finite and positive.")
        if float(quote["even_payout"]) <= float(quote["even_ask_price"]):
            raise ValueError("Even payout must exceed its ask price.")
        if float(quote["odd_payout"]) <= float(quote["odd_ask_price"]):
            raise ValueError("Odd payout must exceed its ask price.")

    ordered_predictions = sorted(
        predictions,
        key=lambda item: (int(item["target_epoch"]), int(item.get("target_index", 0))),
    )
    epoch_counts: dict[int, int] = {}
    for item in ordered_predictions:
        epoch = int(item["target_epoch"])
        epoch_counts[epoch] = epoch_counts.get(epoch, 0) + 1
    ambiguous_epochs = {epoch for epoch, count in epoch_counts.items() if count > 1}
    ambiguous_count = sum(count for count in epoch_counts.values() if count > 1)

    opportunities = len(ordered_predictions)
    unambiguous_count = opportunities - ambiguous_count
    quote_matched = 0
    uncertainty_eligible = 0
    trades = []
    opportunity_records = []
    gross_wins = 0.0
    gross_losses = 0.0
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    expected_values = []
    fold_net: dict[int, float] = {}
    all_folds = {int(item["fold"]) for item in ordered_predictions if type(item.get("fold")) is int}

    for prediction in ordered_predictions:
        epoch = int(prediction["target_epoch"])
        fold = int(prediction.get("fold", 0))
        record = {
            "target_epoch": epoch,
            "fold": fold,
            "qualifies": False,
            "side": None,
            "conservative_expected_value": None,
            "conservative_edge": None,
        }
        if epoch in ambiguous_epochs:
            record["reason"] = "Ambiguous second-resolution tick timestamp."
            opportunity_records.append(record)
            continue

        quote_index = bisect_right(quote_epochs, epoch) - 1
        if quote_index < 0:
            record["reason"] = "No historical quote at or before target tick."
            opportunity_records.append(record)
            continue
        quote = quotes[quote_index]
        quote_age = epoch - int(quote["spot_time"])
        if quote_age < 0 or quote_age > maximum_quote_age_seconds:
            record["reason"] = "Historical quote is stale or future-dated."
            opportunity_records.append(record)
            continue
        quote_matched += 1

        if prediction.get("uncertainty_validated") is not True:
            record["reason"] = "No fold-local validated uncertainty interval."
            opportunity_records.append(record)
            continue
        lower_even = prediction.get("lower_even_probability")
        upper_even = prediction.get("upper_even_probability")
        if (
            isinstance(lower_even, bool) or isinstance(upper_even, bool)
            or not isinstance(lower_even, (int, float))
            or not isinstance(upper_even, (int, float))
            or not isfinite(lower_even) or not isfinite(upper_even)
            or not 0 <= lower_even <= upper_even <= 1
        ):
            record["reason"] = "Probability interval is invalid."
            opportunity_records.append(record)
            continue
        uncertainty_eligible += 1

        side_candidates = []
        for side, lower_probability, ask_key, payout_key in (
            ("even", float(lower_even), "even_ask_price", "even_payout"),
            ("odd", 1.0 - float(upper_even), "odd_ask_price", "odd_payout"),
        ):
            ask = float(quote[ask_key])
            payout = float(quote[payout_key])
            if abs(ask - stake) > max(1e-6, stake * 1e-6):
                continue
            profit = payout - ask
            loss = ask
            break_even = loss / (profit + loss)
            ev = lower_probability * profit - (1 - lower_probability) * loss
            side_candidates.append({
                "side": side,
                "lower_probability": lower_probability,
                "break_even_probability": break_even,
                "edge": lower_probability - break_even,
                "expected_value": ev,
                "profit_if_win": profit,
                "loss_if_lose": loss,
            })
        if not side_candidates:
            record["reason"] = "Quote stake does not match the replay stake."
            opportunity_records.append(record)
            continue

        selected = max(side_candidates, key=lambda item: item["expected_value"])
        record.update({
            "side": selected["side"],
            "conservative_expected_value": selected["expected_value"],
            "conservative_edge": selected["edge"],
        })
        qualifies = (
            selected["edge"] >= minimum_edge
            and selected["expected_value"] >= minimum_expected_value
        )
        if not qualifies:
            record["reason"] = "Conservative edge or EV did not meet the entry threshold."
            opportunity_records.append(record)
            continue
        record["qualifies"] = True
        opportunity_records.append(record)

        actual = prediction.get("actual_parity")
        if actual not in {"even", "odd"}:
            raise ValueError("Every replay prediction must include actual parity.")
        won = actual == selected["side"]
        pnl = selected["profit_if_win"] if won else -selected["loss_if_lose"]
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        if pnl > 0:
            gross_wins += pnl
        else:
            gross_losses += abs(pnl)
        expected_values.append(selected["expected_value"])
        fold_net[fold] = fold_net.get(fold, 0.0) + pnl
        trades.append({
            "target_epoch": epoch,
            "fold": fold,
            "side": selected["side"],
            "actual_parity": actual,
            "outcome": "win" if won else "loss",
            "pnl": pnl,
            "expected_value": selected["expected_value"],
            "conservative_edge": selected["edge"],
            "quote_spot_time": int(quote["spot_time"]),
            "quote_age_seconds": quote_age,
        })

    trade_count = len(trades)
    wins = sum(item["outcome"] == "win" for item in trades)
    losses = trade_count - wins
    total_staked = trade_count * stake
    net_profit = sum(item["pnl"] for item in trades)
    positive_folds = sum(fold_net.get(fold, 0.0) > 0 for fold in all_folds)
    fold_count = len(all_folds)
    positive_fold_rate = positive_folds / fold_count if fold_count else None
    persistence = _estimate_sequence_persistence(opportunity_records)
    return {
        "status": "evaluated" if trade_count else "insufficient_quote_or_uncertainty_coverage",
        "opportunities": opportunities,
        "unambiguous_timestamp_opportunities": unambiguous_count,
        "ambiguous_timestamp_opportunities": ambiguous_count,
        "quote_matched_opportunities": quote_matched,
        "quote_coverage": quote_matched / opportunities if opportunities else 0.0,
        "uncertainty_eligible_opportunities": uncertainty_eligible,
        "entry_count": trade_count,
        "entry_coverage": trade_count / opportunities if opportunities else 0.0,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / trade_count if trade_count else None,
        "stake": stake,
        "total_staked": total_staked,
        "net_profit": net_profit if trade_count else None,
        "roi_on_staked": net_profit / total_staked if total_staked else None,
        "gross_profit": gross_wins,
        "gross_loss": gross_losses,
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
        "maximum_drawdown": max_drawdown if trade_count else None,
        "average_conservative_expected_value": sum(expected_values) / trade_count if trade_count else None,
        "positive_folds": positive_folds,
        "fold_count": fold_count,
        "positive_fold_rate": positive_fold_rate,
        "minimum_trades_for_evidence": minimum_trades_for_evidence,
        "evidence_sample_sufficient": trade_count >= minimum_trades_for_evidence and fold_count >= 5,
        "sequence_persistence_by_horizon": persistence,
        "validated": False,
        "execution_authorized": False,
        "trades": trades,
        "warning": (
            "Historical replay is research evidence only. It uses only matched historical Deriv quote snapshots "
            "and prior-fold uncertainty bounds; it does not prove future profitability or authorize execution."
        ),
    }
