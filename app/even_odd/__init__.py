"""Independent Even/Odd research and decision domain.

This package deliberately does not import the legacy digit strategy, market
analysis, MT5 bridge, or shared trading execution modules.
"""

from app.even_odd.policies import EvenOddPolicy
from app.even_odd.sequence_optimizer import (
    CandidateTradeForecast,
    SequenceDecision,
    select_sequence_length,
)
from app.even_odd.sequence_manager import (
    Revalidation,
    SequenceManager,
    SequenceState,
    TradeOutcome,
)

__all__ = [
    "EvenOddPolicy",
    "CandidateTradeForecast",
    "SequenceDecision",
    "select_sequence_length",
    "Revalidation",
    "SequenceManager",
    "SequenceState",
    "TradeOutcome",
]
