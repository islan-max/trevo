from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def round_money(value: Any) -> Decimal:
    return to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def distribute_installments(total: Any, installments: int) -> list[Decimal]:
    if installments <= 0:
        raise ValueError("Quantidade de parcelas deve ser maior que zero.")

    rounded_total = round_money(total)
    total_cents = int((rounded_total * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if total_cents < installments:
        raise ValueError("Valor total insuficiente para a quantidade de parcelas.")

    base = round_money(Decimal(total_cents // installments) / Decimal("100"))
    parts = [base for _ in range(installments)]
    parts[-1] = round_money(rounded_total - sum(parts[:-1], Decimal("0")))
    return parts


def apply_installment_interest(total: Any, installments: int, interest_rate_percent: Any) -> list[Decimal]:
    """Distribui ``total`` em parcelas aplicando juros simples sobre saldo médio.

    ``interest_rate_percent`` é uma TAXA (ex.: ``1.99`` para 1,99% a.m.), não
    dinheiro — nunca arredonde-a com ``round_money`` antes de chamar esta
    função: isso a quantiza para centavos, e taxas abaixo de 0,5% a.m.
    (comuns em consórcio e parcelamento promocional) somem por completo,
    fazendo os juros desaparecerem (DOM-02). Só o resultado final em dinheiro
    é arredondado.

    A fórmula (juros simples sobre saldo médio: total × (1 + taxa × n / 2))
    é uma aproximação, não amortização real (Tabela Price) — isso é uma
    escolha de produto já assumida antes desta função existir; o que ela
    corrige é apenas o arredondamento da taxa.
    """
    rate_percent = to_decimal(interest_rate_percent)
    if rate_percent <= 0:
        return distribute_installments(total, installments)

    rate = rate_percent / Decimal("100")
    total_with_interest = round_money(to_decimal(total) * (Decimal("1") + rate * Decimal(installments) / Decimal("2")))
    return distribute_installments(total_with_interest, installments)


def format_brl(value: Any) -> str:
    formatted = f"{round_money(value):,.2f}"
    return "R$ " + formatted.replace(",", "X").replace(".", ",").replace("X", ".")
