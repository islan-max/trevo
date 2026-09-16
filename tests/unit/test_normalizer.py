from __future__ import annotations

from decimal import Decimal

import pytest

from app.integrations.normalizer import parse_decimal_text


def test_parse_decimal_text_detects_en_us_thousands_separator():
    """CSV-01: quando ',' e '.' aparecem juntos, o separador que aparece
    por último é o decimal — não se assume mais sempre formato pt-BR."""
    assert parse_decimal_text("1,234.56") == Decimal("1234.56")


def test_parse_decimal_text_detects_pt_br_thousands_separator():
    assert parse_decimal_text("1.234,56") == Decimal("1234.56")


def test_parse_decimal_text_recognizes_parentheses_as_negative():
    """CSV-02: valor negativo entre parênteses, comum em exports de cartão."""
    assert parse_decimal_text("(123,45)") == Decimal("-123.45")


def test_parse_decimal_text_recognizes_trailing_sign_as_negative():
    """CSV-03: sinal posposto, usado por alguns exports de banco."""
    assert parse_decimal_text("1.234,56-") == Decimal("-1234.56")


def test_parse_decimal_text_recognizes_leading_sign_as_negative():
    assert parse_decimal_text("-123,45") == Decimal("-123.45")


def test_parse_decimal_text_handles_currency_prefix_and_plain_comma():
    assert parse_decimal_text("R$ 3.000,00") == Decimal("3000.00")
    assert parse_decimal_text("R$ -100,00") == Decimal("-100.00")


def test_parse_decimal_text_handles_plain_dot_decimal():
    assert parse_decimal_text("123.45") == Decimal("123.45")


@pytest.mark.parametrize("bad_value", ["", "-", "+", "   ", "abc"])
def test_parse_decimal_text_rejects_values_without_digits(bad_value):
    with pytest.raises(ValueError):
        parse_decimal_text(bad_value)
