from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ConsentPayload(BaseModel):
    scope: str = Field(..., min_length=1, max_length=50)
    granted: bool

    model_config = ConfigDict(extra="forbid")
