from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from app.api.deps import request_cached
from app.auth.service import ensure_user_defaults
from app.core.database import db_cursor
from app.shared.money import round_money
from app.shared.serialization import normalize_row


def get_settings(user_id: str) -> dict:
    return request_cached(("settings", user_id), lambda: _compute_settings(user_id))



def _compute_settings(user_id: str) -> dict:
    with db_cursor() as cursor:
        cursor.execute("SELECT * FROM settings WHERE user_id = %s AND id = 1", (user_id,))
        row = normalize_row(cursor.fetchone())
    if row:
        return row

    ensure_user_defaults(user_id)
    with db_cursor() as cursor:
        cursor.execute("SELECT * FROM settings WHERE user_id = %s AND id = 1", (user_id,))
        row = normalize_row(cursor.fetchone())
    if not row:
        raise HTTPException(status_code=500, detail="Configura\u00e7\u00f5es n\u00e3o encontradas.")
    return row



def get_effective_income(user_settings: dict, inflow: Any) -> Decimal:
    """Renda efetiva do m\u00eas: o maior entre a renda configurada e o que j\u00e1
    entrou em lan\u00e7amentos de receita.

    Antes, get_dashboard, _compute_goals e calculate_score somavam
    monthly_income (renda configurada em Configura\u00e7\u00f5es) a inflow (soma de
    TODAS as transa\u00e7\u00f5es de tipo income do m\u00eas) sem checar se eram a mesma
    coisa. Quem lan\u00e7a o sal\u00e1rio como transa\u00e7\u00e3o de entrada \u2014 o gesto natural,
    e a categoria padr\u00e3o "Sal\u00e1rio" existe exatamente para isso \u2014 tinha a
    renda contada duas vezes: or\u00e7amento dispon\u00edvel e meta di\u00e1ria dobravam, o
    Ritmo Score inflava e os alertas de estouro paravam de disparar (DOM-05).

    monthly_income passa a ser tratado como renda ESPERADA: se o que j\u00e1
    entrou no m\u00eas cobre ou supera esse valor, usa o que entrou; caso
    contr\u00e1rio usa o configurado (para quem ainda n\u00e3o lan\u00e7ou a renda do m\u00eas
    corrente, ou lan\u00e7a s\u00f3 parte dela como transa\u00e7\u00e3o).
    """
    monthly_income = round_money(user_settings.get("monthly_income") or 0)
    return max(monthly_income, round_money(inflow))

