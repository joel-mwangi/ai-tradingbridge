"""Hard safety limits for the independent Even/Odd strategy."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvenOddPolicy:
    """User-approved trade-count limits plus conservative execution defaults.

    Stake and monetary loss caps are intentionally not assigned here. They
    must be configured from the account/session risk budget before execution.
    """

    max_trades_per_sequence: int = 7
    max_consecutive_losses: int = 2
    max_trades_per_session: int = 10
    fixed_stake: bool = True
    martingale_enabled: bool = False
    max_concurrent_contracts: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.max_trades_per_sequence <= 7:
            raise ValueError("max_trades_per_sequence must be between 1 and 7.")
        if self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be at least 1.")
        if self.max_trades_per_session < 1:
            raise ValueError("max_trades_per_session must be at least 1.")
        if self.max_trades_per_session < self.max_trades_per_sequence:
            raise ValueError(
                "max_trades_per_session cannot be lower than the per-sequence maximum."
            )
        if not self.fixed_stake:
            raise ValueError("Even/Odd execution requires fixed stake sizing.")
        if self.martingale_enabled:
            raise ValueError("Martingale is prohibited for Even/Odd execution.")
        if self.max_concurrent_contracts != 1:
            raise ValueError("Only one active contract is permitted.")
