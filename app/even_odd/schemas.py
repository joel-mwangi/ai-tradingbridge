"""Request contracts for the independent Even/Odd API."""

from pydantic import BaseModel, Field, StrictInt


class CollectTicksRequest(BaseModel):
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_]{2,32}$")
    tick_count: StrictInt = Field(default=50, ge=10, le=300)
    timeout_seconds: StrictInt = Field(default=60, ge=5, le=120)


class AnalyzeDigitsRequest(BaseModel):
    digits: list[StrictInt] = Field(min_length=1, max_length=5000)
