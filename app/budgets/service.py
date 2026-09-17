from __future__ import annotations

from decimal import Decimal

from app.api.deps import request_cached
from app.core.database import db_cursor
from app.integrations.normalizer import normalize_duplicate_text
from app.shared.dates import get_current_month
from app.shared.money import round_money
from app.shared.serialization import normalize_rows
from app.shared.validation import validate_month_text


def get_budget_status(spent: Decimal, planned: Decimal) -> str:
    if planned <= 0:
        return "ok"
    if spent >= planned:
        return "over"
    if spent >= planned * Decimal("0.80"):
        return "attention"
    return "ok"



def get_budget_summary(user_id: str, month: str) -> dict:
    return request_cached(("budget", user_id, month), lambda: _compute_budget_summary(user_id, month))



def _compute_budget_summary(user_id: str, month: str) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              b.id,
              b.category_id,
              c.name AS category_name,
              c.color AS category_color,
              c.icon AS category_icon,
              b.planned_amount,
              COALESCE(SUM(t.amount), 0) AS spent
            FROM budgets b
            JOIN categories c ON c.id = b.category_id AND c.user_id = b.user_id
            LEFT JOIN transactions t
              ON t.user_id = b.user_id
             AND t.category_id = b.category_id
             AND t.type = 'expense'
             AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = b.month
            WHERE b.user_id = %s
              AND b.month = %s
              AND COALESCE(c.is_active, TRUE) = TRUE
            GROUP BY b.id, b.category_id, c.name, c.color, c.icon, b.planned_amount
            ORDER BY c.name ASC
            """,
            (user_id, month_key),
        )
        rows = normalize_rows(cursor.fetchall())

        cursor.execute(
            """
            SELECT c.id, c.name, c.color, c.icon, COALESCE(SUM(t.amount), 0) AS spent
            FROM categories c
            LEFT JOIN transactions t
              ON t.user_id = c.user_id
             AND t.category_id = c.id
             AND t.type = 'expense'
             AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            WHERE c.user_id = %s
              AND c.type = 'expense'
              AND COALESCE(c.is_active, TRUE) = TRUE
              AND NOT EXISTS (
                SELECT 1
                FROM budgets b
                WHERE b.user_id = c.user_id AND b.category_id = c.id AND b.month = %s
              )
            GROUP BY c.id, c.name, c.color, c.icon
            ORDER BY spent DESC, c.name ASC
            """,
            (month_key, user_id, month_key),
        )
        unbudgeted = normalize_rows(cursor.fetchall())

    items: list[dict] = []
    total_planned = Decimal("0")
    total_spent = Decimal("0")
    for row in rows:
        planned = round_money(row["planned_amount"])
        spent = round_money(row["spent"])
        total_planned += planned
        total_spent += spent
        progress = float(min(Decimal("100"), (spent / planned) * Decimal("100"))) if planned > 0 else 0.0
        status_name = get_budget_status(spent, planned)
        items.append(
            {
                "id": row["id"],
                "categoryId": row["category_id"],
                "categoryName": row["category_name"],
                "categoryColor": row["category_color"],
                "categoryIcon": row["category_icon"],
                "plannedAmount": planned,
                "spent": spent,
                "remaining": round_money(planned - spent),
                "progress": progress,
                "status": status_name,
            }
        )

    return {
        "month": month_key,
        "totalPlanned": round_money(total_planned),
        "totalSpent": round_money(total_spent),
        "remaining": round_money(total_planned - total_spent),
        "items": items,
        "unbudgetedCategories": [
            {
                "categoryId": row["id"],
                "categoryName": row["name"],
                "categoryColor": row["color"],
                "categoryIcon": row["icon"],
                "spent": round_money(row["spent"]),
            }
            for row in unbudgeted
        ],
    }



def list_categorization_rules(user_id: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT r.id, r.category_id, r.payment_method, r.pattern, c.name AS category_name
            FROM categorization_rules r
            JOIN categories c ON c.id = r.category_id AND c.user_id = r.user_id
            WHERE r.user_id = %s
            ORDER BY length(r.pattern) DESC, r.created_at ASC
            """,
            (user_id,),
        )
        return normalize_rows(cursor.fetchall())



def find_matching_rule(rules: list[dict], description: str) -> dict | None:
    normalized_description = normalize_duplicate_text(description)
    for rule in rules:
        if normalize_duplicate_text(rule["pattern"]) in normalized_description:
            return rule
    return None



def match_categorization_rule(user_id: str, description: str) -> dict | None:
    return find_matching_rule(list_categorization_rules(user_id), description)

