from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PinPayload(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6)

    model_config = ConfigDict(extra="forbid")


class CardPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    brand: str = Field(..., min_length=1, max_length=40)
    lastFour: str = Field(..., min_length=4, max_length=4)
    creditLimit: Decimal = Field(..., ge=0, le=999999999)
    closingDay: int = Field(..., ge=1, le=31)
    dueDay: int = Field(..., ge=1, le=31)
    color: str = Field(default="#171717", min_length=1, max_length=20)

    model_config = ConfigDict(extra="forbid")


class CardUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    brand: str | None = Field(default=None, min_length=1, max_length=40)
    lastFour: str | None = Field(default=None, min_length=4, max_length=4)
    creditLimit: Decimal | None = Field(default=None, ge=0, le=999999999)
    closingDay: int | None = Field(default=None, ge=1, le=31)
    dueDay: int | None = Field(default=None, ge=1, le=31)
    color: str | None = Field(default=None, min_length=1, max_length=20)

    model_config = ConfigDict(extra="forbid")


class InstallmentPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    categoryId: int | None = Field(default=None, ge=1)
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    notes: str = Field(default="", max_length=1000)

    model_config = ConfigDict(extra="forbid")


class PurchaseSimulationPayload(BaseModel):
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    months: int = Field(default=12, ge=1, le=24)

    model_config = ConfigDict(extra="forbid")
