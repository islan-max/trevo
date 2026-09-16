from __future__ import annotations

from decimal import Decimal

import pytest

from app.main import add_months, build_duplicate_hash, parse_decimal_text, parse_import_date, parse_import_type
from app.shared.dates import get_month_range


def test_add_months_forward_and_backward():
    assert add_months("2024-12", 1) == "2025-01"
    assert add_months("2024-01", -1) == "2023-12"


def test_month_range_regular_month():
    assert get_month_range("2024-04") == ("2024-04-01", "2024-04-30")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-06-01", "2026-06-01"),
        ("01/06/2026", "2026-06-01"),
        ("01-06-2026", "2026-06-01"),
    ],
)
def test_parse_import_date_accepts_expected_formats(raw, expected):
    assert parse_import_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100,00", Decimal("100.00")),
        ("-100,00", Decimal("-100.00")),
        ("R$ 100,00", Decimal("100.00")),
        ("R$ -100,00", Decimal("-100.00")),
        ("1.250,90", Decimal("1250.90")),
        ("-1.250,90", Decimal("-1250.90")),
    ],
)
def test_parse_decimal_text_accepts_brazilian_values(raw, expected):
    assert parse_decimal_text(raw) == expected


def test_parse_import_type_detects_income_and_expense():
    assert parse_import_type(None, Decimal("100")) == "income"
    assert parse_import_type(None, Decimal("-100")) == "expense"
    assert parse_import_type("entrada", Decimal("-100")) == "income"
    assert parse_import_type("saida", Decimal("100")) == "expense"


def test_duplicate_hash_includes_transaction_type():
    income_hash = build_duplicate_hash("user-1", "2026-06-01", "Ajuste", Decimal("100"), "income")
    expense_hash = build_duplicate_hash("user-1", "2026-06-01", "Ajuste", Decimal("100"), "expense")
    legacy_hash = build_duplicate_hash("user-1", "2026-06-01", "Ajuste", Decimal("100"))

    assert income_hash != expense_hash
    assert legacy_hash != income_hash
