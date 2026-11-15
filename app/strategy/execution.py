"""Demo-only sequential execution state for confirmed digit strategy signals."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from app.strategy.digit_strategy import Group, barrier_valid_for

if TYPE_CHECKING:
    from app.strategy.market_scanner import ScanResult


class ExecutionState(str, Enum):
    IDLE = "idle"
    WAITING_PROPOSAL = "waiting_proposal"
    WAITING_BUY = "waiting_buy"
    WAITING_SETTLEMENT = "waiting_settlement"
    COMPLETED = "completed"
    STOPPED = "stopped"
    BLOCKED = "blocked"


@dataclass
class StrategyExecution:
    """Coordinate at most three sequential demo buys for one strategy signal."""

    symbol: str
    group: Group
    barrier: int
    stake: float = 1.0
    currency: str = "USD"
    duration: int = 1
    max_buys: int = 3
    state: ExecutionState = ExecutionState.IDLE
    buy_count: int = 0
    consecutive_losses: int = 0
    pending_proposal_request: int | None = None
    pending_buy_request: int | None = None
    active_contract_id: str | None = None
    active_contract_sub_id: str | None = None
    signal_accepted: bool = False
    contract_type_override: str | None = None  # set for EVEN/ODD/MATCHES/DIFFERS

    def __post_init__(self) -> None:
        # When an override is set the barrier constraint is not applied
        # (EVEN/ODD have no barrier, and the override carries the real type).
        if self.contract_type_override is None:
            if not self.symbol or not barrier_valid_for(self.group, self.barrier):
                raise ValueError("Execution requires a valid symbol, direction, and barrier.")
        else:
            if not self.symbol:
                raise ValueError("Execution requires a valid symbol.")
        if not isinstance(self.stake, (int, float)) or isinstance(self.stake, bool):
            raise ValueError("Execution stake must be numeric.")
        if not math.isfinite(float(self.stake)) or float(self.stake) <= 0:
            raise ValueError("Execution stake must be positive and finite.")
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise ValueError("Execution currency is required.")
        if type(self.duration) is not int or not 1 <= self.duration <= 10:
            raise ValueError("Execution duration must be between 1 and 10 ticks.")
        if type(self.max_buys) is not int or self.max_buys != 3:
            raise ValueError("Strategy execution must allow exactly three buys maximum.")

    @classmethod
    def from_scan_result(
        cls,
        scan_result: "ScanResult",
        stake: float,
        currency: str,
        duration: int,
    ) -> "StrategyExecution":
        """Factory: create a StrategyExecution from a ScanResult."""
        ct = scan_result.spec.contract_type
        # Map OVER/UNDER directly; all others use Group.OVER as a placeholder
        # and store the real contract type in contract_type_override.
        if ct == "DIGITOVER":
            group = Group.OVER
            barrier = scan_result.spec.barrier if scan_result.spec.barrier is not None else 5
            override = None
        elif ct == "DIGITUNDER":
            group = Group.UNDER
            barrier = scan_result.spec.barrier if scan_result.spec.barrier is not None else 5
            override = None
        else:
            group = Group.OVER  # placeholder
            # EVEN/ODD: barrier is irrelevant; use 0 as placeholder
            # MATCHES/DIFFERS: barrier is the target digit
            barrier = scan_result.spec.barrier if scan_result.spec.barrier is not None else 0
            override = ct
        return cls(
            symbol=scan_result.spec.symbol,
            group=group,
            barrier=barrier,
            stake=stake,
            currency=currency,
            duration=duration,
            contract_type_override=override,
        )

    @property
    def contract_type(self) -> str:
        if self.contract_type_override is not None:
            return self.contract_type_override
        return "DIGITOVER" if self.group is Group.OVER else "DIGITUNDER"

    def status(self, reason: str) -> dict[str, Any]:
        return {
            "type": "strategy_execution_status",
            "state": self.state.value,
            "reason": reason,
            "symbol": self.symbol,
            "direction": self.group.value,
            "barrier": self.barrier,
            "buy_count": self.buy_count,
            "max_buys": self.max_buys,
            "consecutive_losses": self.consecutive_losses,
            "active_contract_id": self.active_contract_id,
        }

    def reset_for_next_signal(self) -> None:
        """Allow a new signal to be accepted after a completed trade sequence."""
        if self.state in (ExecutionState.IDLE, ExecutionState.COMPLETED, ExecutionState.STOPPED):
            self.signal_accepted = False

    def begin_signal(self, request_id: int) -> dict[str, Any] | None:
        if self.signal_accepted or self.state not in (ExecutionState.IDLE, ExecutionState.COMPLETED):
            return None
        self.signal_accepted = True
        self.state = ExecutionState.WAITING_PROPOSAL
        self.pending_proposal_request = request_id
        return self._proposal(request_id)
    def stop(self) -> None:
        self.state = ExecutionState.STOPPED
        self.pending_proposal_request = None
        self.pending_buy_request = None
        self.active_contract_id = None
        self.active_contract_sub_id = None

    def on_proposal(
        self,
        request_id: int,
        proposal: dict[str, Any],
        buy_request_id: int,
    ) -> dict[str, Any] | None:
        if self.state is not ExecutionState.WAITING_PROPOSAL or request_id != self.pending_proposal_request:
            return None
        proposal_id = proposal.get("id")
        ask_price = proposal.get("ask_price")
        if not isinstance(proposal_id, (str, int)) or not str(proposal_id).strip():
            return self._block("Deriv returned a proposal without an identifier.")
        try:
            quoted_price = float(ask_price)
        except (TypeError, ValueError):
            quoted_price = 0.0
        if not math.isfinite(quoted_price) or quoted_price <= 0:
            return self._block("Deriv returned an invalid proposal price.")
        self.pending_proposal_request = None
        self.pending_buy_request = buy_request_id
        self.state = ExecutionState.WAITING_BUY
        return {
            "buy": str(proposal_id),
            "price": quoted_price,
            "req_id": buy_request_id,
        }

    def on_buy(self, request_id: int, response: dict[str, Any]) -> dict[str, Any] | None:
        if self.state is not ExecutionState.WAITING_BUY or request_id != self.pending_buy_request:
            return None
        buy = response.get("buy")
        contract_id = buy.get("contract_id") if isinstance(buy, dict) else None
        if not isinstance(contract_id, (str, int)) or not str(contract_id).strip():
            return self._block("Deriv buy acknowledgement did not include a contract ID.")
        self.pending_buy_request = None
        self.active_contract_id = str(contract_id)
        self.active_contract_sub_id = None
        self.buy_count += 1
        self.state = ExecutionState.WAITING_SETTLEMENT
        return {
            "proposal_open_contract": 1,
            "contract_id": self.active_contract_id,
            "subscribe": 1,
        }

    def on_settlement(
        self,
        contract_id: str,
        status: str | None,
        profit: Any,
        next_request_id: int,
        confirmed_settled: bool = False,
    ) -> dict[str, Any] | None:
        if self.state is not ExecutionState.WAITING_SETTLEMENT or contract_id != self.active_contract_id:
            return None
        outcome = self._outcome(status, profit)
        if outcome is None:
            if not confirmed_settled:
                return None
            # Contract is confirmed settled by is_sold/status flags but profit is
            # unparseable — treat as a loss to avoid an indefinite stall.
            import logging
            logging.getLogger(__name__).warning(
                "Settlement profit unparseable for contract %s (status=%s, profit=%r); treating as loss.",
                contract_id, status, profit,
            )
            outcome = "loss"
        self.active_contract_id = None
        if outcome == "loss":
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        if self.consecutive_losses >= 2:
            self.state = ExecutionState.STOPPED
            return None
        if self.buy_count >= self.max_buys:
            self.state = ExecutionState.COMPLETED
            return None

        self.state = ExecutionState.WAITING_PROPOSAL
        self.pending_proposal_request = next_request_id
        return self._proposal(next_request_id)

    def block(self, reason: str) -> None:
        return self._block(reason)

    def _proposal(self, request_id: int) -> dict[str, Any]:
        ct = self.contract_type
        payload: dict[str, Any] = {
            "proposal": 1,
            "amount": float(self.stake),
            "basis": "stake",
            "contract_type": ct,
            "currency": self.currency.upper(),
            "duration": self.duration,
            "duration_unit": "t",
            "underlying_symbol": self.symbol,
            "req_id": request_id,
        }
        # EVEN and ODD: no barrier field
        if ct in ("DIGITEVEN", "DIGITODD"):
            pass  # omit barrier intentionally
        # MATCHES and DIFFERS: barrier is the target digit integer
        elif ct in ("DIGITMATCH", "DIGITDIFF"):
            payload["barrier"] = self.barrier
        # OVER and UNDER: existing logic unchanged
        else:
            payload["barrier"] = self.barrier
        return payload

    def _block(self, reason: str) -> None:
        self.state = ExecutionState.BLOCKED
        self.pending_proposal_request = None
        self.pending_buy_request = None
        self.active_contract_id = None
        self.active_contract_sub_id = None

    @staticmethod
    def _outcome(status: str | None, profit: Any) -> str | None:
        normalized = status.lower() if isinstance(status, str) else ""
        if normalized in {"lost", "loss"}:
            return "loss"
        if normalized in {"won", "win"}:
            try:
                numeric_profit = float(profit)
            except (TypeError, ValueError):
                numeric_profit = 0.0
            return "loss" if numeric_profit < 0 else "win"
        try:
            numeric_profit = float(profit)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric_profit):
            return None
        if numeric_profit < 0:
            return "loss"
        if normalized in {"open", "pending", ""}:
            return None
        return "win"
