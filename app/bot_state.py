"""Fail-closed state machine for Demo bot execution.

This module intentionally contains no broker I/O. The execution worker must persist each
transition before sending a side-effecting request and must reconcile OUTCOME_UNKNOWN with
Deriv before it can issue another buy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TradeState(StrEnum):
    CREATED = "created"
    REQUESTING_PROPOSAL = "requesting_proposal"
    BUY_PENDING = "buy_pending"
    OPEN = "open"
    SETTLING = "settling"
    SETTLED = "settled"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


_ALLOWED: dict[TradeState, set[TradeState]] = {
    TradeState.CREATED: {TradeState.REQUESTING_PROPOSAL, TradeState.FAILED},
    TradeState.REQUESTING_PROPOSAL: {TradeState.BUY_PENDING, TradeState.FAILED},
    TradeState.BUY_PENDING: {TradeState.OPEN, TradeState.FAILED, TradeState.OUTCOME_UNKNOWN},
    TradeState.OPEN: {TradeState.SETTLING, TradeState.OUTCOME_UNKNOWN},
    TradeState.SETTLING: {TradeState.SETTLED, TradeState.OUTCOME_UNKNOWN},
    TradeState.SETTLED: set(),
    TradeState.FAILED: set(),
    TradeState.OUTCOME_UNKNOWN: {TradeState.OPEN, TradeState.SETTLING, TradeState.SETTLED, TradeState.FAILED},
}


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    state: TradeState
    contract_id: str | None = None
    proposal_id: str | None = None
    stake: float | None = None
    payout: float | None = None
    profit: float | None = None


def transition(snapshot: TradeSnapshot, target: TradeState, **updates: object) -> TradeSnapshot:
    """Validate a transition and preserve broker identifiers unless explicitly updated."""
    if target not in _ALLOWED[snapshot.state]:
        raise ValueError(f"Illegal trade-state transition: {snapshot.state} -> {target}")

    values: dict[str, object] = {
        "state": target,
        "contract_id": snapshot.contract_id,
        "proposal_id": snapshot.proposal_id,
        "stake": snapshot.stake,
        "payout": snapshot.payout,
        "profit": snapshot.profit,
    }
    unknown = set(updates) - (set(values) - {"state"})
    if unknown:
        raise ValueError(f"Unsupported trade fields: {', '.join(sorted(unknown))}")
    values.update(updates)

    if target in {TradeState.OPEN, TradeState.SETTLING, TradeState.SETTLED} and not values["contract_id"]:
        raise ValueError(f"{target} requires a confirmed contract_id")
    if target == TradeState.SETTLED:
        if values["profit"] is None or values["payout"] is None:
            raise ValueError("SETTLED requires broker-confirmed payout and profit")
    if target == TradeState.OUTCOME_UNKNOWN and snapshot.state not in {
        TradeState.BUY_PENDING, TradeState.OPEN, TradeState.SETTLING
    }:
        raise ValueError("Only a possibly accepted buy/open contract can have unknown outcome")
    return TradeSnapshot(**values)  # type: ignore[arg-type]


def may_submit_buy(snapshot: TradeSnapshot) -> bool:
    """A buy is allowed only after a fresh proposal and before any buy has been attempted."""
    return snapshot.state == TradeState.REQUESTING_PROPOSAL and bool(snapshot.proposal_id)


def may_start_next_trade(snapshot: TradeSnapshot) -> bool:
    """Never start a subsequent trade until the prior one is conclusively settled or failed."""
    return snapshot.state in {TradeState.SETTLED, TradeState.FAILED}
