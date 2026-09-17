from __future__ import annotations

from app.api.deps import request_cached
from app.core.database import db_cursor
from app.shared.serialization import normalize_rows


def list_categories(user_id: str) -> list[dict]:
    return request_cached(("categories", user_id), lambda: _compute_categories(user_id))



def _compute_categories(user_id: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM categories
            WHERE user_id = %s
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY type ASC, name ASC
            """,
            (user_id,),
        )
        return normalize_rows(cursor.fetchall())

