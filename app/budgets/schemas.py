from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class BudgetPayload(BaseModel):
    categoryId: int = Field(..., ge=1)
    month: str = Field(..., min_length=7, max_length=7)
    plannedAmount: Decimal = Field(..., ge=0, le=999999999)

    class Config:
        extra = "forbid"


class BudgetCopyPayload(BaseModel):
    fromMonth: str = Field(..., min_length=7, max_length=7)
    toMonth: str = Field(..., min_length=7, max_length=7)

    class Config:
        extra = "forbid"


class CategorizationRulePayload(BaseModel):
    pattern: str = Field(..., min_length=2, max_length=120)
    categoryId: int = Field(..., ge=1)
    paymentMethod: str | None = Field(default=None, min_length=1, max_length=50)

    class Config:
        extra = "forbid"
