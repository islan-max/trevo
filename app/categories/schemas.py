from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CategoryPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    type: Literal["income", "expense"] = "expense"
    # CFG-03: era #9be768, o verde da marca anterior — o atual é #2E9D5B.
    color: str = Field(default="#2E9D5B", min_length=1, max_length=20)
    icon: str = Field(default="●", min_length=1, max_length=10)

    model_config = ConfigDict(extra="forbid")
