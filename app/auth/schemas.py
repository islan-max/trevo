from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RegisterPayload(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=72)
    name: str = Field(..., min_length=1, max_length=100)
    accept_terms: bool = False

    model_config = ConfigDict(extra="forbid")


class DeleteAccountPayload(BaseModel):
    password: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProfilePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=500)
    send_monthly_summary: bool | None = None

    model_config = ConfigDict(extra="forbid")


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=72)
    new_password: str = Field(..., min_length=8, max_length=72)

    model_config = ConfigDict(extra="forbid")
