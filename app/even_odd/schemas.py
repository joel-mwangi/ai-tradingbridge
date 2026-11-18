"""Request contracts for the independent Even/Odd API."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class CollectTicksRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$")
    tick_count: StrictInt = Field(default=50, ge=10, le=300)
    timeout_seconds: StrictInt = Field(default=60, ge=5, le=80)


class AnalyzeDigitsRequest(BaseModel):
    digits: list[StrictInt] = Field(min_length=1, max_length=5000)



class CreateSessionRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$")
    account_mode: Literal["research", "demo", "real"] = "research"
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    fixed_stake: Decimal = Field(gt=0)
    max_session_loss: Decimal = Field(gt=0)
    max_trades_per_session: StrictInt = Field(default=10, ge=1, le=10)
    max_consecutive_losses: StrictInt = Field(default=2, ge=1, le=2)


class SequencePlanObservationRequest(BaseModel):
    tick_row_id: StrictInt = Field(gt=0)
    tick_epoch: StrictInt = Field(gt=0)


class StartSequenceRequest(BaseModel):
    session_id: UUID
    model_id: UUID
    model_version: str = Field(min_length=1, max_length=128)
    observations: list[SequencePlanObservationRequest] = Field(min_length=3, max_length=7)


class RevalidationEvidence(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_validated: StrictBool
    uncertainty_validated: StrictBool
    signal_valid: StrictBool
    data_fresh: StrictBool
    risk_budget_available: StrictBool


class RecordTradeSettlementRequest(BaseModel):
    sequence_id: UUID
    trade_number: StrictInt = Field(ge=1, le=7)
    side: Literal["even", "odd"]
    stake: Decimal = Field(gt=0)
    profit_if_win: Decimal = Field(gt=0)
    loss_if_lose: Decimal = Field(gt=0)
    even_probability: Decimal = Field(ge=0, le=1)
    lower_even_probability: Decimal = Field(ge=0, le=1)
    upper_even_probability: Decimal = Field(ge=0, le=1)
    conservative_expected_value: Decimal
    revalidation: RevalidationEvidence
    outcome: Literal["win", "loss"]
    realized_profit: Decimal
    external_contract_id: str | None = Field(default=None, max_length=128)



class EntryObservationRequest(BaseModel):
    tick_row_id: StrictInt = Field(gt=0)
    tick_epoch: StrictInt = Field(gt=0)
    model_id: UUID
    model_version: str = Field(min_length=1, max_length=128)



class EvaluateEntryRequest(BaseModel):
    session_id: UUID
    observations: list[EntryObservationRequest] = Field(min_length=3, max_length=3)



class PlanSequenceRequest(BaseModel):
    session_id: UUID
    model_id: UUID
    model_version: str = Field(min_length=1, max_length=128)
    observations: list[SequencePlanObservationRequest] = Field(min_length=1, max_length=7)
