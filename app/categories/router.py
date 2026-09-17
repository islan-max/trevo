from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from psycopg2 import errors

from app.api.deps import PlainDictRoute, get_current_user
from app.categories.schemas import CategoryPayload
from app.core.database import db_cursor
from app.shared.serialization import normalize_row, require_row
from app.shared.validation import clean_text, validate_hex_color

router = APIRouter(route_class=PlainDictRoute)


@router.post("/api/categories")
def create_category(payload: CategoryPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    name = clean_text(payload.name, "Nome da categoria", 80)
    color = validate_hex_color(payload.color)
    icon = clean_text(payload.icon, "\u00cdcone", 10)

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "SELECT id, is_active FROM categories WHERE user_id = %s AND name = %s",
                (user_id, name),
            )
            existing = normalize_row(cursor.fetchone())

            if existing and existing["is_active"]:
                # SEC-03: o antigo ON CONFLICT ... DO UPDATE SET type = EXCLUDED.type
                # reescrevia o type de uma categoria ATIVA existente, reclassificando
                # em massa todo o hist\u00f3rico ligado a ela. Nome j\u00e1 em uso por uma
                # categoria ativa \u00e9 conflito, n\u00e3o atualiza\u00e7\u00e3o.
                raise HTTPException(status_code=409, detail="Categoria j\u00e1 existe.")

            if existing:
                # Reativa a categoria arquivada. O type NUNCA muda aqui pelo
                # mesmo motivo acima \u2014 s\u00f3 is_active, color e icon acompanham a
                # escolha atual do usu\u00e1rio.
                cursor.execute(
                    """
                    UPDATE categories
                    SET color = %s, icon = %s, is_active = TRUE, updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (color, icon, existing["id"]),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO categories (user_id, name, type, color, icon, is_default, is_active)
                    VALUES (%s, %s, %s, %s, %s, 0, TRUE)
                    RETURNING *
                    """,
                    (user_id, name, payload.type, color, icon),
                )
            row = require_row(normalize_row(cursor.fetchone()), "Categoria n\u00e3o criada.")
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Categoria j\u00e1 existe.") from None

    return row



@router.delete("/api/categories/{category_id}")
def delete_category(category_id: int, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            SELECT id, is_default
            FROM categories
            WHERE user_id = %s AND id = %s AND COALESCE(is_active, TRUE) = TRUE
            """,
            (user_id, category_id),
        )
        category = normalize_row(cursor.fetchone())
        if not category:
            raise HTTPException(status_code=404, detail="Categoria n\u00e3o encontrada.")

        cursor.execute(
            "SELECT COUNT(*) AS total FROM transactions WHERE user_id = %s AND category_id = %s",
            (user_id, category_id),
        )
        linked_transactions = int(require_row(normalize_row(cursor.fetchone()), "V\u00ednculos n\u00e3o encontrados.")["total"])

        cursor.execute("DELETE FROM budgets WHERE user_id = %s AND category_id = %s", (user_id, category_id))
        cursor.execute("DELETE FROM categorization_rules WHERE user_id = %s AND category_id = %s", (user_id, category_id))

        should_archive = linked_transactions > 0 or int(category.get("is_default") or 0) == 1
        if should_archive:
            cursor.execute(
                "UPDATE categories SET is_active = FALSE WHERE user_id = %s AND id = %s",
                (user_id, category_id),
            )
            return {"deleted": False, "archived": True, "linkedTransactions": linked_transactions}

        cursor.execute("DELETE FROM categories WHERE user_id = %s AND id = %s", (user_id, category_id))
        return {"deleted": True, "archived": False, "linkedTransactions": 0}

