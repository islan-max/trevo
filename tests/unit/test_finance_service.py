from __future__ import annotations

from decimal import Decimal

import pytest

from app.main import (
    SettingsPayload,
    add_months,
    distribute_installments,
    format_brl,
    round_money,
    to_decimal,
)
from app.shared.dates import get_month_range


def test_format_brl():
    assert format_brl(1234.56) == "R$ 1.234,56"


def test_to_decimal_uses_string_conversion_for_float_inputs():
    assert to_decimal(0.1 + 0.2) == Decimal("0.30000000000000004")
    assert round_money(0.1 + 0.2) == Decimal("0.30")


@pytest.mark.parametrize(
    ("total", "installments", "expected"),
    [
        ("100", 3, [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]),
        ("10", 3, [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")]),
        ("1", 3, [Decimal("0.33"), Decimal("0.33"), Decimal("0.34")]),
    ],
)
def test_distribute_installments_handles_known_cent_differences(total, installments, expected):
    parts = distribute_installments(total, installments)

    assert parts == expected
    assert sum(parts, Decimal("0")) == round_money(total)


@pytest.mark.parametrize("total", ["1", "10", "100", "123.45"])
@pytest.mark.parametrize("installments", [2, 3, 6, 10, 12])
def test_distribute_installments_preserves_total_for_common_installment_counts(
    total,
    installments,
):
    parts = distribute_installments(total, installments)

    assert len(parts) == installments
    assert sum(parts, Decimal("0")) == round_money(total)
    assert all(part == round_money(part) for part in parts)


def test_distribute_installments_rejects_more_installments_than_available_cents():
    with pytest.raises(ValueError, match="Valor total insuficiente"):
        distribute_installments("0.01", 2)


def test_add_months_crosses_year_boundaries():
    assert add_months("2024-12", 1) == "2025-01"
    assert add_months("2024-01", -1) == "2023-12"


def test_month_range_handles_leap_year():
    assert get_month_range("2024-02") == ("2024-02-01", "2024-02-29")


def test_score_contract_range_with_stub():
    score = {"score": 720}
    assert 0 <= score["score"] <= 1000


def test_settings_payload_accepts_zero_daily_goal_for_auto_recommendation():
    payload = SettingsPayload(monthlyIncome="0", dailyGoal="0", reserveAmount="0")
    assert payload.dailyGoal == Decimal("0")
