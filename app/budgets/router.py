from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import errors

from app.api.deps import PlainDictRoute, get_current_user
from app.budgets.schemas import BudgetCopyPayload, BudgetPayload, CategorizationRulePayload
from app.budgets.service import get_budget_summary
from app.core.database import db_cursor
from app.integrations.normalizer import normalize_duplicate_text
from app.shared.dates import get_current_month
from app.shared.money import round_money
from app.shared.serialization import normalize_row, normalize_rows, require_row
from app.shared.validation import clean_text, validate_month_text

router = APIRouter(route_class=PlainDictRoute)


@router.get("/api/budgets")
def budgets(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return get_budget_summary(current_user["id"], month_key)



@router.post("/api/budgets")
def save_budget(payload: BudgetPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    month_key = validate_month_text(payload.month) or get_current_month()
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO budgets (user_id, category_id, month, planned_amount)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, category_id, month)
                DO UPDATE SET planned_amount = EXCLUDED.planned_amount, updated_at = NOW()
                RETURNING *
                """,
                (user_id, payload.categoryId, month_key, round_money(payload.plannedAmount)),
            )
            row = require_row(normalize_row(cursor.fetchone()), "Orçamento não salvo.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria inválida.") from None
    return row



@router.delete("/api/budgets/{budget_id}")
def delete_budget(budget_id: int, current_user: dict = Depends(get_current_user)) -> dict:
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            "DELETE FROM budgets WHERE user_id = %s AND id = %s RETURNING id",
            (current_user["id"], budget_id),
        )
        row = normalize_row(cursor.fetchone())
    if not row:
        raise HTTPException(status_code=404, detail="Or\u00e7amento n\u00e3o encontrado.")
    return {"deleted": True}



@router.post("/api/budgets/copy")
def copy_budget(payload: BudgetCopyPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    from_month = validate_month_text(payload.fromMonth) or get_current_month()
    to_month = validate_month_text(payload.toMonth) or get_current_month()
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            INSERT INTO budgets (user_id, category_id, month, planned_amount)
            SELECT b.user_id, b.category_id, %s, b.planned_amount
            FROM budgets b
            JOIN categories c ON c.id = b.category_id AND c.user_id = b.user_id
            WHERE b.user_id = %s
              AND b.month = %s
              AND COALESCE(c.is_active, TRUE) = TRUE
            ON CONFLICT (user_id, category_id, month)
            DO UPDATE SET planned_amount = EXCLUDED.planned_amount, updated_at = NOW()
            """,
            (to_month, user_id, from_month),
        )
    return get_budget_summary(user_id, to_month)



@router.get("/api/categorization-rules")
def categorization_rules(current_user: dict = Depends(get_current_user)) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT r.*, c.name AS category_name
            FROM categorization_rules r
            JOIN categories c ON c.id = r.category_id AND c.user_id = r.user_id
            WHERE r.user_id = %s
            ORDER BY r.created_at DESC
            """,
            (current_user["id"],),
        )
        return normalize_rows(cursor.fetchall())



@router.post("/api/categorization-rules")
def create_categorization_rule(
    payload: CategorizationRulePayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    pattern = normalize_duplicate_text(clean_text(payload.pattern, "Padrao", 120))
    payment_method = (
        clean_text(payload.paymentMethod, "Forma de pagamento", 50, required=False)
        if payload.paymentMethod
        else None
    )
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO categorization_rules (user_id, pattern, category_id, payment_method)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, pattern)
                DO UPDATE SET category_id = EXCLUDED.category_id, payment_method = EXCLUDED.payment_method
                RETURNING *
                """,
                (user_id, pattern, payload.categoryId, payment_method),
            )
            return require_row(normalize_row(cursor.fetchone()), "Regra não criada.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria inválida.") from None

