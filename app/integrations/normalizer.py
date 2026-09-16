from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Any

from app.shared.money import round_money, to_decimal


def normalize_duplicate_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def build_duplicate_hash(
    user_id: str,
    transaction_date: str,
    description: str,
    amount: Any,
    transaction_type: str = "",
    time: str | None = None,
    account: str | None = None,
) -> str:
    """Hash de deduplicação da importação de CSV.

    ``time`` e ``account`` são opcionais e só entram no hash quando
    informados (FIN-01) — sem eles, duas transações legítimas e distintas no
    mesmo dia (mesma descrição e valor, hora diferente) produziam o mesmo
    hash e uma era descartada como duplicata. Omitir os dois reproduz
    exatamente o formato anterior, usado para consultar lançamentos
    importados antes desta mudança.
    """
    amount_text = f"{round_money(amount):.2f}"
    parts = [user_id, transaction_date, normalize_duplicate_text(description), amount_text]
    if transaction_type:
        parts.append(transaction_type)
    if time:
        parts.append(time)
    if account:
        parts.append(normalize_duplicate_text(account))
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_decimal_text(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Valor vazio.")

    # CSV-02: negativo entre parênteses, comum em exports de cartão e contábeis.
    negative = text.startswith("(") and text.rstrip().endswith(")")

    cleaned = re.sub(r"[^\d,.\-+]", "", text)

    # CSV-03: sinal posposto ("1.234,56-"), usado por alguns exports de banco.
    if cleaned.endswith("-"):
        negative = True
        cleaned = cleaned[:-1]
    elif cleaned.endswith("+"):
        cleaned = cleaned[:-1]

    if not any(char.isdigit() for char in cleaned):
        raise ValueError("Valor inválido.")

    if "," in cleaned and "." in cleaned:
        # CSV-01: o separador que aparece por ÚLTIMO é o decimal. Antes
        # assumia-se sempre formato pt-BR (milhar=ponto, decimal=vírgula),
        # então um valor em formato en-US ("1,234.56") virava 1,23456 — mil
        # vezes menor. Agora "1,234.56" e "1.234,56" resolvem certo, cada
        # um pela posição dos separadores.
        cleaned = (
            cleaned.replace(".", "").replace(",", ".")
            if cleaned.rindex(",") > cleaned.rindex(".")
            else cleaned.replace(",", "")
        )
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    result = to_decimal(cleaned)
    return -abs(result) if negative else result
