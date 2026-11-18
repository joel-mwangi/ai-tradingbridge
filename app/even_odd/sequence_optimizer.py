"""Select a trade-sequence length from validated ML forecasts.

This is a deterministic decision layer, not a trained ML model. A separately
trained and validated model must supply one conditional forecast per potential
trade. The optimizer refuses to recommend execution when model validation or
forecast evidence is missing.
"""

from dataclasses import dataclass
from math import isfinite

from app.even_odd.policies import EvenOddPolicy


@dataclass(frozen=True, slots=True)
class CandidateTradeForecast:
    """Forecast for the next trade, conditional on reaching that trade."""

    trade_number: int
    win_probability: float
    lower_bound_win_probability: float
    profit_if_win: float
    loss_if_lose: float
    signal_valid: bool
    model_validated: bool
    uncertainty_validated: bool
    model_id: str
    model_version: str

    def __post_init__(self) -> None:
        if type(self.trade_number) is not int or self.trade_number < 1:
            raise ValueError("trade_number must be a positive integer.")
        for name, value in (
            ("win_probability", self.win_probability),
            ("lower_bound_win_probability", self.lower_bound_win_probability),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be a finite probability from 0 to 1.")
        if self.lower_bound_win_probability > self.win_probability:
            raise ValueError("Lower-bound probability cannot exceed the point estimate.")
        for name, value in (
            ("profit_if_win", self.profit_if_win),
            ("loss_if_lose", self.loss_if_lose),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive.")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("A model ID is required.")
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ValueError("A model version is required.")
        if (
            type(self.signal_valid) is not bool
            or type(self.model_validated) is not bool
            or type(self.uncertainty_validated) is not bool
        ):
            raise ValueError(
                "signal_valid, model_validated and uncertainty_validated must be booleans."
            )

    @property
    def expected_value(self) -> float:
        """Point-estimate EV, in the same monetary unit as the stake outcomes."""
        return (
            self.win_probability * self.profit_if_win
            - (1 - self.win_probability) * self.loss_if_lose
        )

    @property
    def conservative_expected_value(self) -> float:
        """EV using the model's lower confidence bound for win probability."""
        p = self.lower_bound_win_probability
        return p * self.profit_if_win - (1 - p) * self.loss_if_lose


@dataclass(frozen=True, slots=True)
class SequenceDecision:
    selected_trades: int
    max_allowed_trades: int
    expected_value_by_trade: tuple[float, ...]
    conservative_ev_by_trade: tuple[float, ...]
    model_id: str | None
    model_version: str | None
    approved: bool
    reason: str


def select_sequence_length(
    forecasts: list[CandidateTradeForecast],
    *,
    policy: EvenOddPolicy = EvenOddPolicy(),
    session_trades_remaining: int | None = None,
) -> SequenceDecision:
    """Choose the longest positive-EV prefix, capped by policy and session.

    Forecasts are conditional forecasts for trade 1, trade 2, etc. The first
    invalid, unvalidated, non-positive conservative-EV, mismatched-model, or
    out-of-order forecast terminates the candidate sequence. Every subsequent
    trade still requires a fresh forecast; this initial decision is not
    authorization to execute all selected trades.
    """
    session_cap = (
        policy.max_trades_per_session
        if session_trades_remaining is None
        else max(0, session_trades_remaining)
    )
    cap = min(policy.max_trades_per_sequence, session_cap, len(forecasts))

    if cap == 0:
        return SequenceDecision(
            0, min(policy.max_trades_per_sequence, session_cap), (), (), None, None,
            False, "No session trade capacity or no candidate forecasts.",
        )

    selected = 0
    point_evs: list[float] = []
    conservative_evs: list[float] = []
    selected_model: CandidateTradeForecast | None = None
    stop_reason = "Maximum permitted sequence length reached."

    for index, forecast in enumerate(forecasts[:cap], start=1):
        if forecast.trade_number != index:
            stop_reason = "Forecast sequence is incomplete or out of order."
            break
        if not forecast.model_validated:
            stop_reason = "The forecasting model has not passed validation."
            break
        if not forecast.uncertainty_validated:
            stop_reason = "The model probability uncertainty has not passed validation."
            break
        if selected_model and (
            forecast.model_id != selected_model.model_id
            or forecast.model_version != selected_model.model_version
        ):
            stop_reason = "Forecast model identity/version changed within the sequence."
            break
        if not forecast.signal_valid:
            stop_reason = f"Opportunity is not valid for candidate trade {index}."
            break
        if forecast.conservative_expected_value <= 0:
            stop_reason = (
                f"Candidate trade {index} has non-positive conservative expected value."
            )
            break

        selected += 1
        point_evs.append(forecast.expected_value)
        conservative_evs.append(forecast.conservative_expected_value)
        selected_model = forecast

    if selected == 0:
        return SequenceDecision(
            0, cap, tuple(point_evs), tuple(conservative_evs),
            selected_model.model_id if selected_model else None,
            selected_model.model_version if selected_model else None,
            False, stop_reason,
        )

    return SequenceDecision(
        selected_trades=selected,
        max_allowed_trades=cap,
        expected_value_by_trade=tuple(point_evs),
        conservative_ev_by_trade=tuple(conservative_evs),
        model_id=selected_model.model_id if selected_model else None,
        model_version=selected_model.model_version if selected_model else None,
        approved=True,
        reason=stop_reason,
    )
