from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import HTTPException

from app.core.database import db_cursor
from app.shared.dates import add_months
from app.shared.money import round_money
from app.shared.serialization import normalize_rows


def list_transactions(
    user_id: str,
    month: str | None = None,
    transaction_type: str | None = None,
    category_id: int | None = None,
    payment_method: str | None = None,
    source: str | None = None,
    card_id: int | None = None,
    search: str | None = None,
    limit: int = 250,
    offset: int = 0,
) -> tuple[list[dict], bool]:
    """Retorna (linhas da página, há mais depois dela).

    DB-08: sem paginação, a lista inteira do filtro vinha truncada num
    LIMIT 250 fixo, sem indicar ao cliente que havia mais linhas. Pede
    limit + 1 para saber se há próxima página sem uma segunda query de
    COUNT(*).
    """
    pattern = f"%{search}%" if search else None
    query = """
        SELECT t.*, c.name AS category_name, c.color AS category_color, cards.name AS card_name
        FROM transactions t
        LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
        LEFT JOIN cards ON cards.id = t.card_id AND cards.user_id = t.user_id
        WHERE t.user_id = %s
          AND (%s IS NULL OR COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s)
          AND (%s IS NULL OR t.type = %s)
          AND (%s IS NULL OR t.category_id = %s)
          AND (%s IS NULL OR t.payment_method = %s)
          AND (%s IS NULL OR t.source = %s)
          AND (%s IS NULL OR t.card_id = %s)
          AND (
            %s IS NULL
            OR lower(t.title) LIKE lower(%s)
            OR lower(COALESCE(t.raw_description, '')) LIKE lower(%s)
          )
        ORDER BY t.transaction_date DESC, t.id DESC
        LIMIT %s OFFSET %s
    """
    params = (
        user_id,
        month,
        month,
        transaction_type,
        transaction_type,
        category_id,
        category_id,
        payment_method,
        payment_method,
        source,
        source,
        card_id,
        card_id,
        pattern,
        pattern,
        pattern,
        limit + 1,
        offset,
    )

    with db_cursor() as cursor:
        cursor.execute(query, params)
        rows = normalize_rows(cursor.fetchall())

    has_more = len(rows) > limit
    return rows[:limit], has_more



def normalize_recurrence(
    is_recurring: bool,
    recurrence_type: str | None,
    recurrence_day: int | None,
    transaction_date: str | None = None,
) -> tuple[bool, str | None, int | None]:
    if not is_recurring:
        return False, None, None

    if recurrence_type not in ("monthly", "weekly"):
        raise HTTPException(status_code=400, detail="Tipo de recorr\u00eancia inv\u00e1lido.")

    if recurrence_day is None and transaction_date:
        parsed = datetime.strptime(transaction_date, "%Y-%m-%d").date()
        recurrence_day = parsed.day if recurrence_type == "monthly" else parsed.weekday()

    if recurrence_day is None:
        raise HTTPException(status_code=400, detail="Dia da recorr\u00eancia \u00e9 obrigat\u00f3rio.")

    if recurrence_type == "monthly" and not 1 <= recurrence_day <= 31:
        raise HTTPException(status_code=400, detail="Dia mensal deve ficar entre 1 e 31.")
    if recurrence_type == "weekly" and not 0 <= recurrence_day <= 6:
        raise HTTPException(status_code=400, detail="Dia semanal deve ficar entre 0 e 6.")

    return True, recurrence_type, recurrence_day



def suggested_dates_for_recurrence(month: str, recurrence_type: str, recurrence_day: int) -> list[str]:
    from calendar import monthrange

    year, month_num = [int(part) for part in month.split("-")]
    total_days = monthrange(year, month_num)[1]
    if recurrence_type == "monthly":
        return [date(year, month_num, min(recurrence_day, total_days)).isoformat()]

    dates: list[str] = []
    for day in range(1, total_days + 1):
        current = date(year, month_num, day)
        if current.weekday() == recurrence_day:
            dates.append(current.isoformat())
    return dates



def get_recurring_suggestions(user_id: str, month: str) -> list[dict]:
    previous_month = add_months(month, -1)
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, title, amount, type, category_id, payment_method, notes, card_id,
                   is_recurring, recurrence_type, recurrence_day
            FROM transactions
            WHERE user_id = %s
              AND is_recurring = TRUE
              AND recurrence_type IS NOT NULL
              AND recurrence_day IS NOT NULL
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            ORDER BY transaction_date ASC, id ASC
            """,
            (user_id, previous_month),
        )
        recurring_rows = normalize_rows(cursor.fetchall())

        suggestions: list[dict] = []
        seen: set[tuple[Any, ...]] = set()
        for row in recurring_rows:
            dates = suggested_dates_for_recurrence(month, row["recurrence_type"], int(row["recurrence_day"]))
            for suggested_date in dates:
                key = (row["title"], round_money(row["amount"]), row["category_id"], suggested_date)
                if key in seen:
                    continue
                seen.add(key)

                cursor.execute(
                    """
                    SELECT 1
                    FROM transactions
                    WHERE user_id = %s
                      AND title = %s
                      AND amount = %s
                      AND COALESCE(category_id, 0) = COALESCE(%s, 0)
                      AND transaction_date = %s
                    LIMIT 1
                    """,
                    (user_id, row["title"], row["amount"], row["category_id"], suggested_date),
                )
                if cursor.fetchone():
                    continue

                suggestions.append(
                    {
                        "title": row["title"],
                        "amount": round_money(row["amount"]),
                        "type": row["type"],
                        "category_id": row["category_id"],
                        "payment_method": row["payment_method"],
                        "card_id": row["card_id"],
                        "suggested_date": suggested_date,
                        "notes": row.get("notes") or "",
                        "is_recurring": True,
                        "recurrence_type": row["recurrence_type"],
                        "recurrence_day": row["recurrence_day"],
                    }
                )

    return suggestions

