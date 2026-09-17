from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class SettingsPayload(BaseModel):
    monthlyIncome: Decimal | None = Field(default=None, ge=0, le=999999999)
    dailyGoal: Decimal | None = Field(default=None, ge=0, le=999999999)
    reserveAmount: Decimal | None = Field(default=None, ge=0, le=999999999)
    reserveGoalAmount: Decimal | None = Field(default=None, ge=0, le=999999999)
    reserveCurrentAmount: Decimal | None = Field(default=None, ge=0, le=999999999)

    model_config = ConfigDict(extra="forbid")
