from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TransactionPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    amount: Decimal = Field(..., gt=0, le=999999999)
    type: Literal["income", "expense"] = "expense"
    categoryId: int | None = Field(default=None, ge=1)
    paymentMethod: str = Field(default="pix", min_length=1, max_length=50)
    transactionDate: str = Field(..., min_length=10, max_length=10)
    notes: str = Field(default="", max_length=1000)
    cardId: int | None = Field(default=None, ge=1)
    billingMonth: str | None = Field(default=None, min_length=7, max_length=7)
    isRecurring: bool = False
    recurrenceType: Literal["monthly", "weekly"] | None = None
    recurrenceDay: int | None = Field(default=None, ge=0, le=31)

    model_config = ConfigDict(extra="forbid")


class TransactionUpdatePayload(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, gt=0, le=999999999)
    type: Literal["income", "expense"] | None = None
    categoryId: int | None = Field(default=None, ge=1)
    paymentMethod: str | None = Field(default=None, min_length=1, max_length=50)
    transactionDate: str | None = Field(default=None, min_length=10, max_length=10)
    notes: str | None = Field(default=None, max_length=1000)
    cardId: int | None = Field(default=None, ge=1)
    billingMonth: str | None = Field(default=None, min_length=7, max_length=7)

    model_config = ConfigDict(extra="forbid")


class RecurringPayload(BaseModel):
    is_recurring: bool
    recurrence_type: Literal["monthly", "weekly"] | None = None
    recurrence_day: int | None = Field(default=None, ge=0, le=31)

    model_config = ConfigDict(extra="forbid")


class InstallmentSimulationPayload(BaseModel):
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    interestRate: float = Field(default=0, ge=0, le=100)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    months: int = Field(default=12, ge=1, le=24)

    model_config = ConfigDict(extra="forbid")


class InstallmentWithoutCardPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    categoryId: int | None = Field(default=None, ge=1)
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    interestRate: float = Field(default=0, ge=0, le=100)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    notes: str = Field(default="", max_length=1000)

    model_config = ConfigDict(extra="forbid")
