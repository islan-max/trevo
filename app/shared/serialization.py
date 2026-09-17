from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.shared.money import round_money


def serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return round_money(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def normalize_row(row: Any | None) -> dict | None:
    if row is None:
        return None
    return {key: serialize_value(value) for key, value in dict(row).items()}


def normalize_rows(rows: list[Any]) -> list[dict]:
    return [normalize_row(row) or {} for row in rows]


def require_row(row: dict | None, detail: str = "Registro não encontrado.") -> dict:
    if row is None:
        raise HTTPException(status_code=500, detail=detail)
    return row
