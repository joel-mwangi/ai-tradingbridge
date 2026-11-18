"""Request contracts for the independent Even/Odd API."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt


class CollectTicksRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$")
    tick_count: StrictInt = Field(default=50, ge=10, le=300)
    timeout_seconds: StrictInt = Field(default=60, ge=5, le=120)


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


class StartSequenceRequest(BaseModel):
    session_id: UUID
    model_id: UUID
    model_version: str = Field(min_length=1, max_length=128)
    planned_trades: StrictInt = Field(ge=1, le=7)
    expected_value_snapshot: dict = Field(default_factory=dict)


class RevalidationEvidence(BaseModel):
    model_validated: bool
    uncertainty_validated: bool
    signal_valid: bool
    data_fresh: bool
    risk_budget_available: bool


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
