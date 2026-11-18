"""Runtime guardrails for Even/Odd trade sequences and sessions."""

from dataclasses import dataclass, field
from enum import Enum

from app.even_odd.policies import EvenOddPolicy


class TradeOutcome(str, Enum):
    WIN = "win"
    LOSS = "loss"


@dataclass(frozen=True, slots=True)
class Revalidation:
    model_validated: bool
    signal_valid: bool
    data_fresh: bool
    conservative_expected_value: float
    risk_budget_available: bool


@dataclass(slots=True)
class SequenceState:
    planned_trades: int
    completed_trades: int = 0
    consecutive_losses: int = 0
    wins: int = 0
    losses: int = 0
    stopped: bool = False
    stop_reason: str | None = None


@dataclass(slots=True)
class SequenceManager:
    """Enforces hard limits; ML recommendations cannot override these limits."""

    policy: EvenOddPolicy = field(default_factory=EvenOddPolicy)
    session_trades: int = 0
    session_losses: int = 0
    session_wins: int = 0
    active_sequence: SequenceState | None = None

    def start_sequence(self, planned_trades: int) -> SequenceState:
        if self.active_sequence and not self.active_sequence.stopped:
            raise ValueError("An active sequence already exists.")
        if not 1 <= planned_trades <= self.policy.max_trades_per_sequence:
            raise ValueError("ML-selected sequence length must be between 1 and 7.")
        if self.session_trades >= self.policy.max_trades_per_session:
            raise ValueError("Session trade limit has been reached.")
        remaining = self.policy.max_trades_per_session - self.session_trades
        if planned_trades > remaining:
            raise ValueError("Planned sequence exceeds remaining session capacity.")
        self.active_sequence = SequenceState(planned_trades=planned_trades)
        return self.active_sequence

    def can_execute_next(self, revalidation: Revalidation) -> tuple[bool, str]:
        state = self.active_sequence
        if state is None:
            return False, "No active sequence."
        if state.stopped:
            return False, state.stop_reason or "Sequence has stopped."
        if state.completed_trades >= state.planned_trades:
            return False, "ML-selected sequence length has been reached."
        if self.session_trades >= self.policy.max_trades_per_session:
            return False, "Session trade limit has been reached."
        if state.consecutive_losses >= self.policy.max_consecutive_losses:
            return False, "Consecutive-loss limit has been reached."
        if not revalidation.model_validated:
            return False, "Model is not validated."
        if not revalidation.data_fresh:
            return False, "Market data is stale."
        if not revalidation.signal_valid:
            return False, "Opportunity is no longer valid."
        if revalidation.conservative_expected_value <= 0:
            return False, "Conservative expected value is not positive."
        if not revalidation.risk_budget_available:
            return False, "Risk budget is exhausted."
        return True, "Execution conditions passed; one trade may be submitted."

    def record_outcome(self, outcome: TradeOutcome) -> SequenceState:
        state = self.active_sequence
        if state is None or state.stopped:
            raise ValueError("There is no active sequence.")
        if state.completed_trades >= state.planned_trades:
            raise ValueError("The planned sequence is already complete.")
        if self.session_trades >= self.policy.max_trades_per_session:
            raise ValueError("The session trade limit has already been reached.")

        state.completed_trades += 1
        self.session_trades += 1
        if outcome == TradeOutcome.WIN:
            state.wins += 1
            self.session_wins += 1
            state.consecutive_losses = 0
        elif outcome == TradeOutcome.LOSS:
            state.losses += 1
            self.session_losses += 1
            state.consecutive_losses += 1
        else:
            raise ValueError("Unknown trade outcome.")

        if state.consecutive_losses >= self.policy.max_consecutive_losses:
            self.stop_sequence("Two consecutive losses.")
        elif state.completed_trades >= state.planned_trades:
            self.stop_sequence("ML-selected sequence length completed.")
        elif self.session_trades >= self.policy.max_trades_per_session:
            self.stop_sequence("Session trade limit reached.")
        return state

    def stop_sequence(self, reason: str) -> None:
        if self.active_sequence is not None:
            self.active_sequence.stopped = True
            self.active_sequence.stop_reason = reason
