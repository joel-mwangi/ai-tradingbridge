"""Validated configuration models for Demo bot runs.

Shared by the bot runner WebSocket endpoint and any future tooling.
No database or broker I/O — pure Pydantic validation only.
"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr


CONTRACTS = {"DIGITOVER", "DIGITUNDER", "DIGITEVEN", "DIGITODD", "DIGITMATCH", "DIGITDIFF"}


class FixedStakeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern="^fixed$")
    amount: float = Field(gt=0, le=1000)


class StopCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern="^(profit|loss|tradeCount|time)$")
    target: float | None = Field(default=None, gt=0)
    limit: float | None = Field(default=None, gt=0)
    max: int | None = Field(default=None, ge=1, le=20)
    seconds: int | None = Field(default=None, ge=1, le=1800)


class DemoBotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr = Field(min_length=1, max_length=80)
    symbol: StrictStr = Field(pattern=r"^[A-Za-z0-9_]{2,32}$")
    contractType: StrictStr
    barrier: str | None = Field(default=None, pattern=r"^\d$")
    duration: int = Field(ge=1, le=10)
    durationUnit: str = Field(pattern="^t$")
    stakeStrategy: FixedStakeConfig
    stopConditions: list[StopCondition] = Field(min_length=1, max_length=4)
    cooldownSeconds: int = Field(default=2, ge=1, le=60)
    maxTradesPerDay: int = Field(default=20, ge=1, le=20)
    maxConsecutiveLosses: int = Field(default=3, ge=1, le=3)

    def validate_contract(self) -> None:
        if self.contractType not in CONTRACTS:
            raise HTTPException(status_code=422, detail="Unsupported digit contract.")
        needs_barrier = self.contractType in {"DIGITOVER", "DIGITUNDER", "DIGITMATCH", "DIGITDIFF"}
        if needs_barrier != (self.barrier is not None):
            raise HTTPException(status_code=422, detail="Barrier does not match contract type.")
        if self.contractType == "DIGITOVER" and int(self.barrier or "0") > 8:
            raise HTTPException(status_code=422, detail="Over barrier must be 0–8.")
        if self.contractType == "DIGITUNDER" and int(self.barrier or "9") < 1:
            raise HTTPException(status_code=422, detail="Under barrier must be 1–9.")
        for stop in self.stopConditions:
            required = {"profit": "target", "loss": "limit", "tradeCount": "max", "time": "seconds"}[stop.type]
            if getattr(stop, required) is None:
                raise HTTPException(status_code=422, detail=f"Stop condition {stop.type} requires {required}.")
        if not any(s.type in {"loss", "tradeCount", "time"} for s in self.stopConditions):
            raise HTTPException(status_code=422, detail="At least one loss, trade-count, or time limit is required.")


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: StrictStr = Field(min_length=2, max_length=32)
    config: DemoBotConfig
