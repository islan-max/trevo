from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CsvColumnMapping(BaseModel):
    date: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1, max_length=120)
    value: str = Field(..., min_length=1, max_length=120)
    type: str | None = Field(default=None, max_length=120)
    # Opcionais: quando o arquivo traz a categoria e a conta/origem, elas são
    # aproveitadas em vez de todo lançamento cair em "sem categoria".
    category: str | None = Field(default=None, max_length=120)
    account: str | None = Field(default=None, max_length=120)
    time: str | None = Field(default=None, max_length=120)

    model_config = ConfigDict(extra="forbid")


class CsvImportPreviewPayload(BaseModel):
    importToken: str = Field(..., min_length=16, max_length=200)
    mapping: CsvColumnMapping

    model_config = ConfigDict(extra="forbid")


class CsvImportConfirmPayload(CsvImportPreviewPayload):
    # "merge" mantém o que já existe e ignora duplicatas; "replace" troca os
    # lançamentos dos meses presentes no arquivo. O default é o modo seguro.
    mode: Literal["merge", "replace"] = "merge"
