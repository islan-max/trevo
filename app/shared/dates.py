from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.shared.clock import current_month as _current_month


def pad(value: int) -> str:
    return str(value).zfill(2)


def month_key_from_date(date_str: str) -> str:
    return date_str[:7]


def add_months(month_key: str, offset: int) -> str:
    year, month = [int(part) for part in month_key.split("-")]
    total_month = (year * 12 + (month - 1)) + offset
    new_year = total_month // 12
    new_month = total_month % 12 + 1
    return f"{new_year}-{pad(new_month)}"


def format_month_label(month_key: str) -> str:
    names = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    year, month = [int(part) for part in month_key.split("-")]
    return f"{names[month - 1]}/{str(year)[2:]}"


def get_current_month() -> str:
    # DOM-04: antes usava datetime.now(UTC) diretamente — o mês contábil
    # trocava até 3h antes da virada real em America/Sao_Paulo (UTC-3).
    return _current_month()


def get_month_range(month_key: str) -> tuple[str, str]:
    year, month = [int(part) for part in month_key.split("-")]
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    start = date(year, month, 1)
    end = date(next_year, next_month, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def as_utc_datetime(value: Any) -> datetime | None:
    """Normaliza para datetime em UTC, aceitando também texto ISO-8601.

    As linhas passam por ``normalize_row``, que serializa datetime como string
    ISO. Sem aceitar esse formato aqui, a função devolvia ``None`` para toda
    coluna vinda do banco e as checagens que dependem dela eram silenciosamente
    puladas: o token continuava válido após troca de senha e o bloqueio por
    tentativas de login nunca era aplicado.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def first_billing_month(purchase_date: str, closing_day: int | None) -> str:
    """Fatura em que a compra cai, considerando o fechamento do cartão.

    Compra feita no dia do fechamento ou antes entra na fatura do próprio mês;
    depois dele, escorrega para a seguinte. Sem ``closing_day`` (compra avulsa,
    sem cartão) o mês da compra é usado como está.
    """
    base_month = month_key_from_date(purchase_date)
    if not closing_day:
        return base_month
    try:
        purchase_day = int(purchase_date[8:10])
    except (ValueError, IndexError):
        return base_month
    return add_months(base_month, 1) if purchase_day > int(closing_day) else base_month
