from __future__ import annotations

import re
from datetime import datetime

from fastapi import HTTPException

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_text(value: str, field_name: str, max_length: int, required: bool = True) -> str:
    cleaned = value.strip()
    if required and not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} é obrigatório.")
    if len(cleaned) > max_length:
        raise HTTPException(status_code=400, detail=f"{field_name} excede o tamanho permitido.")
    return cleaned


def validate_hex_color(value: str, field_name: str = "Cor") -> str:
    cleaned = clean_text(value, field_name, 20)
    if not HEX_COLOR_RE.match(cleaned):
        raise HTTPException(status_code=400, detail=f"{field_name} deve usar formato hexadecimal #RRGGBB.")
    return cleaned


def validate_optional_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    cleaned = clean_text(value, field_name, 500, required=False)
    if not cleaned:
        return None
    if not (cleaned.startswith("https://") or cleaned.startswith("http://")):
        raise HTTPException(status_code=400, detail=f"{field_name} deve usar http ou https.")
    return cleaned


def validate_date_text(value: str, field_name: str) -> str:
    cleaned = clean_text(value, field_name, 10)
    try:
        datetime.strptime(cleaned, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field_name} inválida.") from None
    return cleaned


def validate_month_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = clean_text(value, "Mês", 7)
    try:
        datetime.strptime(cleaned, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="Mês inválido.") from None
    return cleaned


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 255 or not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="E-mail inválido.")
    return email
