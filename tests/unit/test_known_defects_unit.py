"""Testes que documentam defeitos conhecidos e ainda não corrigidos.

Cada teste aqui é `xfail(strict=True)`: falha hoje pelo motivo descrito, e
`strict=True` faz a suíte quebrar se ele passar sem que alguém remova a marca
— o que serve de lembrete para tirar o xfail no breakpoint que corrige o
defeito, em vez de deixá-lo esquecido como falso-verde permanente.

Ver docs/auditoria-2026-09.md para o achado completo de cada ID.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from freezegun import freeze_time

from app.integrations.normalizer import parse_decimal_text
from app.shared.dates import get_current_month


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CSV-01: quando ',' e '.' aparecem juntos, parse_decimal_text assume "
        "sempre formato pt-BR (milhar=ponto, decimal=vírgula). Um valor no "
        "formato en-US ('1,234.56') vira 1,23456 em vez de 1234.56 — mil "
        "vezes menor."
    ),
)
def test_parse_decimal_text_detects_en_us_thousands_separator():
    assert parse_decimal_text("1,234.56") == Decimal("1234.56")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CSV-02: valor negativo entre parênteses (comum em exports de "
        "cartão) não é reconhecido — o regex de limpeza remove os "
        "parênteses e o valor vira positivo."
    ),
)
def test_parse_decimal_text_recognizes_parentheses_as_negative():
    assert parse_decimal_text("(123,45)") == Decimal("-123.45")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DOM-04: get_current_month() usa datetime.now(UTC). Às 23h30 de "
        "30/09 em America/Sao_Paulo (UTC-3) já são 02h30 de 01/10 em UTC, "
        "então o mês contábil troca 3 horas antes da virada real no Brasil."
    ),
)
def test_current_month_respects_brazil_timezone_not_utc():
    # 30/09/2026 23:30 em America/Sao_Paulo == 01/10/2026 02:30 em UTC.
    with freeze_time("2026-10-01 02:30:00"):
        assert get_current_month() == "2026-09"
