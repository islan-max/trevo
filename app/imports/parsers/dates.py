from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.integrations.normalizer import normalize_duplicate_text

# Formatos aceitos na importação. Data e data+hora são tentadas na ordem; o
# extrato de cada banco escolhe um destes.
_IMPORT_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y")
_IMPORT_DATETIME_FORMATS = tuple(
    f"{date_format}{separator}{time_format}"
    for date_format in _IMPORT_DATE_FORMATS
    for separator in (" ", "T")
    for time_format in ("%H:%M:%S", "%H:%M")
)


def parse_import_datetime(value: Any) -> tuple[str, str | None]:
    """Normaliza a data do extrato para (AAAA-MM-DD, HH:MM ou None).

    Bancos exportam data com e sem hora, em vários separadores. Guardar a hora
    quando ela existe permite ordenar lançamentos do mesmo dia na ordem real.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("Data vazia.")

    for date_format in _IMPORT_DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError:
            continue
        return parsed.date().isoformat(), parsed.strftime("%H:%M")

    for date_format in _IMPORT_DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date().isoformat(), None
        except ValueError:
            continue

    raise ValueError("Data inválida.")



def parse_import_date(value: Any) -> str:
    return parse_import_datetime(value)[0]



def parse_import_time(value: Any) -> str | None:
    """Lê uma coluna de hora separada, quando o arquivo tiver uma."""
    text = str(value or "").strip()
    if not text:
        return None
    for time_format in ("%H:%M:%S", "%H:%M", "%H%M"):
        try:
            return datetime.strptime(text, time_format).strftime("%H:%M")
        except ValueError:
            continue
    return None



def parse_import_type(raw_type: Any, amount: Decimal) -> Literal["income", "expense"]:
    if raw_type is None or str(raw_type).strip() == "":
        return "expense" if amount < 0 else "income"

    value = normalize_duplicate_text(str(raw_type))
    if value in {"income", "entrada", "credito", "crédito", "credit", "receita"}:
        return "income"
    if value in {"expense", "saida", "saída", "debito", "débito", "debit", "despesa"}:
        return "expense"
    return "expense" if amount < 0 else "income"

