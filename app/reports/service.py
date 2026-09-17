from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.budgets.service import get_budget_summary
from app.cards.service import get_cards_summary
from app.core.database import db_cursor
from app.dashboard.service import calculate_score, get_alerts_for_month, get_dashboard
from app.goals.service import get_goals
from app.shared.dates import add_months, get_current_month
from app.shared.money import round_money, to_decimal
from app.shared.serialization import normalize_rows
from app.shared.validation import validate_month_text


def get_reports_summary(user_id: str, month: str) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    dashboard = get_dashboard(user_id, month_key)
    budget = get_budget_summary(user_id, month_key)
    cards = get_cards_summary(user_id, month_key)
    score_data = calculate_score(user_id, month_key)
    goals = get_goals(user_id, month_key)
    category_growth = get_category_growth(user_id, month_key)
    alerts = get_alerts_for_month(user_id, month_key)
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT payment_method, type, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY payment_method, type
            ORDER BY total DESC
            """,
            (user_id, month_key),
        )
        payment_methods = normalize_rows(cursor.fetchall())
    return {
        "month": month_key,
        "dashboard": dashboard,
        "budget": budget,
        "cards": cards,
        "score": score_data,
        "goals": goals,
        "paymentMethods": payment_methods,
        "categoryGrowth": category_growth,
        "alerts": alerts,
    }



def get_category_growth(user_id: str, month: str) -> dict:
    previous_month = add_months(month, -1)
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              COALESCE(c.name, 'Sem categoria') AS name,
              COALESCE(c.color, '#14B8A6') AS color,
              COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.name, c.color
            """,
            (user_id, month),
        )
        current_rows = normalize_rows(cursor.fetchall())

        cursor.execute(
            """
            SELECT
              COALESCE(c.name, 'Sem categoria') AS name,
              COALESCE(c.color, '#14B8A6') AS color,
              COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.name, c.color
            """,
            (user_id, previous_month),
        )
        previous_rows = normalize_rows(cursor.fetchall())

    current_by_name = {row["name"]: row for row in current_rows}
    previous_by_name = {row["name"]: row for row in previous_rows}
    names = sorted(set(current_by_name) | set(previous_by_name))
    items = []
    for name in names:
        current_total = round_money(current_by_name.get(name, {}).get("total") or 0)
        previous_total = round_money(previous_by_name.get(name, {}).get("total") or 0)
        delta = round_money(current_total - previous_total)
        percent_change = None
        if previous_total > 0:
            percent_change = round_money((delta / previous_total) * Decimal("100"))
        color = current_by_name.get(name, previous_by_name.get(name, {})).get("color") or "#14B8A6"
        items.append(
            {
                "name": name,
                "color": color,
                "currentTotal": current_total,
                "previousTotal": previous_total,
                "delta": delta,
                "percentChange": percent_change,
            }
        )

    items.sort(key=lambda item: abs(to_decimal(item["delta"])), reverse=True)
    return {
        "month": month,
        "previousMonth": previous_month,
        "hasHistory": any(round_money(row.get("total") or 0) > 0 for row in previous_rows),
        "items": items,
    }



def payment_method_label(value: Any) -> str:
    labels = {
        "boleto": "Boleto",
        "cash": "Dinheiro",
        "credito": "Crédito",
        "credit": "Crédito",
        "debito": "Débito",
        "debit": "Débito",
        "dinheiro": "Dinheiro",
        "pix": "Pix",
        "transfer": "Transferência",
    }
    text = str(value or "").strip()
    return labels.get(text, text or "Outro")



def transaction_source_label(value: Any) -> str:
    labels = {
        "manual": "Manual",
        "csv_import": "Importação CSV",
        "open_finance_future": "Open Finance",
    }
    text = str(value or "").strip()
    return labels.get(text, text or "Manual")



def get_export_transactions(user_id: str, month_key: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT t.transaction_date, t.title, c.name AS category_name, t.type, t.amount,
                   t.payment_method, t.source, t.notes, cards.name AS card_name,
                   t.installment_number, t.total_installments
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            LEFT JOIN cards ON cards.id = t.card_id AND cards.user_id = t.user_id
            WHERE t.user_id = %s
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            ORDER BY t.transaction_date ASC, t.id ASC
            """,
            (user_id, month_key),
        )
        return normalize_rows(cursor.fetchall())

