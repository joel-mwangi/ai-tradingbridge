"""Pydantic request models and shared account-id pattern for the Deriv gateway."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, SecretStr, StrictStr, field_validator

# Account identifiers are short alphanumeric strings; reused by routes and the
# WebSocket proxy to validate the requested account before opening a session.
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9]{2,32}$")


class CredentialTransfer(BaseModel):
    user_id: StrictStr = Field(min_length=1, max_length=128)
    access_token: SecretStr = Field(min_length=1, max_length=4096)
    expires_in: int = Field(default=3600, ge=60, le=86400)


class AccountRequest(BaseModel):
    account_id: StrictStr = Field(min_length=2, max_length=32)


class OAuthExchangeRequest(BaseModel):
    user_id: StrictStr = Field(min_length=1, max_length=128)
    code: StrictStr = Field(min_length=1, max_length=4096)
    code_verifier: StrictStr = Field(min_length=43, max_length=128)
    redirect_uri: StrictStr = Field(min_length=1, max_length=2048)


class AccountOwnershipRequest(BaseModel):
    user_id: StrictStr = Field(min_length=1, max_length=128)
    account_ids: list[StrictStr] = Field(min_length=1, max_length=100)

    @field_validator("account_ids")
    @classmethod
    def validate_account_ids(cls, value: list[str]) -> list[str]:
        if any(not ACCOUNT_ID_RE.fullmatch(account_id) for account_id in value):
            raise ValueError("Invalid Deriv account ID.")
        return list(dict.fromkeys(value))
