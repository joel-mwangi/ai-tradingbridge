"""Select a sequence length from validated ML forecasts and OOS survival evidence.

The optimizer is a deterministic risk/policy layer, not a standalone trained
sequence model. It combines conditional ML forecasts with fold-aware
same-side opportunity-persistence bounds and realized payout replay evidence.
It refuses to plan when any evidence gate is missing.
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
    risk_budget_available: bool
    model_id: str
    model_version: str
    opportunity_persistence_lower_bound: float = 0.0
    opportunity_persistence_validated: bool = False
    historical_oos_evidence_rows: int = 0
    historical_oos_positive_fold_rate: float = 0.0
    historical_oos_expected_value: float = 0.0
    historical_oos_net_profit: float = 0.0
    historical_oos_roi: float = 0.0

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
            isinstance(self.opportunity_persistence_lower_bound, bool)
            or not isinstance(self.opportunity_persistence_lower_bound, (int, float))
            or not isfinite(self.opportunity_persistence_lower_bound)
            or not 0 <= self.opportunity_persistence_lower_bound <= 1
        ):
            raise ValueError("opportunity_persistence_lower_bound must be a finite probability.")
        if type(self.historical_oos_evidence_rows) is not int or self.historical_oos_evidence_rows < 0:
            raise ValueError("historical_oos_evidence_rows must be a non-negative integer.")
        if (
            isinstance(self.historical_oos_positive_fold_rate, bool)
            or not isinstance(self.historical_oos_positive_fold_rate, (int, float))
            or not isfinite(self.historical_oos_positive_fold_rate)
            or not 0 <= self.historical_oos_positive_fold_rate <= 1
        ):
            raise ValueError("historical_oos_positive_fold_rate must be a finite probability.")
        for name, value in (
            ("historical_oos_expected_value", self.historical_oos_expected_value),
            ("historical_oos_net_profit", self.historical_oos_net_profit),
            ("historical_oos_roi", self.historical_oos_roi),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError(f"{name} must be finite and numeric.")
        if (
            type(self.signal_valid) is not bool
            or type(self.model_validated) is not bool
            or type(self.uncertainty_validated) is not bool
            or type(self.risk_budget_available) is not bool
            or type(self.opportunity_persistence_validated) is not bool
        ):
            raise ValueError(
                "signal, model, uncertainty, persistence and risk flags must be booleans."
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
    sequence_adjusted_conservative_ev_by_trade: tuple[float, ...] = ()
    persistence_lower_bound_by_trade: tuple[float, ...] = ()


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
    sequence_adjusted_evs: list[float] = []
    persistence_bounds: list[float] = []
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
        if not forecast.risk_budget_available:
            stop_reason = "The remaining session loss budget is insufficient."
            break
        if forecast.historical_oos_evidence_rows < 200:
            stop_reason = "Insufficient historical out-of-sample payout evidence."
            break
        if (
            forecast.historical_oos_positive_fold_rate < 0.6
            or forecast.historical_oos_expected_value <= 0
            or forecast.historical_oos_net_profit <= 0
            or forecast.historical_oos_roi <= 0
        ):
            stop_reason = "Historical out-of-sample payout performance is not consistently positive."
            break
        if not forecast.opportunity_persistence_validated:
            stop_reason = "Opportunity persistence is not validated for this sequence horizon."
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
        sequence_adjusted_ev = (
            forecast.opportunity_persistence_lower_bound
            * forecast.conservative_expected_value
        )
        if sequence_adjusted_ev <= 0:
            stop_reason = f"Candidate trade {index} has non-positive persistence-adjusted EV."
            break

        selected += 1
        point_evs.append(forecast.expected_value)
        conservative_evs.append(forecast.conservative_expected_value)
        sequence_adjusted_evs.append(sequence_adjusted_ev)
        persistence_bounds.append(forecast.opportunity_persistence_lower_bound)
        selected_model = forecast

    if selected == 0:
        return SequenceDecision(
            0, cap, tuple(point_evs), tuple(conservative_evs),
            selected_model.model_id if selected_model else None,
            selected_model.model_version if selected_model else None,
            False, stop_reason, tuple(sequence_adjusted_evs), tuple(persistence_bounds),
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
        sequence_adjusted_conservative_ev_by_trade=tuple(sequence_adjusted_evs),
        persistence_lower_bound_by_trade=tuple(persistence_bounds),
    )
