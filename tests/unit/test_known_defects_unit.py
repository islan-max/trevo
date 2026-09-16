"""Testes que documentam defeitos conhecidos e ainda não corrigidos.

Cada teste aqui é `xfail(strict=True)`: falha hoje pelo motivo descrito, e
`strict=True` faz a suíte quebrar se ele passar sem que alguém remova a marca
— o que serve de lembrete para tirar o xfail no breakpoint que corrige o
defeito, em vez de deixá-lo esquecido como falso-verde permanente.

Ver docs/auditoria-2026-09.md para o achado completo de cada ID.

DOM-04 foi corrigido no BP-03 e seu teste promovido para test_clock.py.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.integrations.normalizer import parse_decimal_text


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
