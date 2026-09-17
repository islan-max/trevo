from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CategoryPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    type: Literal["income", "expense"] = "expense"
    color: str = Field(default="#9be768", min_length=1, max_length=20)
    icon: str = Field(default="●", min_length=1, max_length=10)

    class Config:
        extra = "forbid"
