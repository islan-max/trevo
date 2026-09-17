from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg2 import errors

from app.api.deps import PlainDictRoute, get_current_user
from app.cards.schemas import CardPayload, CardUpdatePayload, InstallmentPayload, PinPayload, PurchaseSimulationPayload
from app.cards.service import (
    clear_card_pin_failures,
    enforce_card_pin_rate_limit,
    get_card_for_user,
    get_card_pin_row,
    get_cards_summary,
    get_unlocked_card_details,
    invalidate_card_unlock_sessions,
    record_card_pin_failure,
    simulate_card_invoices,
    verify_card_unlock_session,
)
from app.core.database import db_cursor
from app.core.logging import audit_log
from app.core.security import hash_pin, validate_pin, verify_pin
from app.shared.dates import add_months, first_billing_month, get_current_month, month_key_from_date
from app.shared.money import distribute_installments, round_money
from app.shared.serialization import normalize_row, normalize_rows, require_row
from app.shared.validation import clean_text, validate_date_text, validate_hex_color, validate_month_text

router = APIRouter(route_class=PlainDictRoute)


@router.post("/api/cards/{card_id}/set-pin")
def set_card_pin(card_id: int, payload: PinPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    get_card_for_user(user_id, card_id)
    pin = validate_pin(payload.pin)

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            INSERT INTO card_pins (card_id, user_id, pin_hash)
            VALUES (%s, %s, %s)
            ON CONFLICT (card_id, user_id)
            DO UPDATE SET pin_hash = EXCLUDED.pin_hash, created_at = NOW()
            """,
            (card_id, user_id, hash_pin(pin)),
        )

    clear_card_pin_failures(user_id, card_id)
    invalidate_card_unlock_sessions(user_id, card_id)
    audit_log("card_pin_set", user_id, {"card_id": card_id})
    return {"ok": True}



@router.post("/api/cards/{card_id}/unlock")
def unlock_card(
    card_id: int,
    payload: PinPayload,
    month: str | None = None,
    categoryId: int | None = Query(default=None, ge=1),
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    get_card_for_user(user_id, card_id)
    pin = validate_pin(payload.pin)
    enforce_card_pin_rate_limit(user_id, card_id)

    pin_row = get_card_pin_row(user_id, card_id)
    if not pin_row:
        raise HTTPException(status_code=400, detail="PIN n\u00e3o definido.")

    if not verify_pin(pin, pin_row["pin_hash"]):
        attempts_remaining = record_card_pin_failure(user_id, card_id)
        audit_log("card_pin_wrong", user_id, {"card_id": card_id, "attempts_remaining": attempts_remaining})
        if attempts_remaining <= 0:
            audit_log("card_pin_blocked", user_id, {"card_id": card_id})
            raise HTTPException(
                status_code=429,
                detail="Muitas tentativas. Tente novamente em 5 minutos.",
                headers={"X-Attempts-Remaining": "0"},
            )
        raise HTTPException(
            status_code=401,
            detail="PIN incorreto",
            headers={"X-Attempts-Remaining": str(attempts_remaining)},
        )

    clear_card_pin_failures(user_id, card_id)
    audit_log("card_unlocked", user_id, {"card_id": card_id})
    return get_unlocked_card_details(user_id, card_id, month_key, include_token=True, category_id=categoryId)



@router.get("/api/cards/{card_id}/simulate-invoices")
def simulate_invoices_route(
    card_id: int,
    months: int = Query(12, ge=1, le=24),
    month: str | None = None,
    categoryId: int | None = Query(default=None, ge=1),
    x_card_unlock_token: str = Header(..., alias="X-Card-Unlock-Token"),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    get_card_for_user(user_id, card_id)
    verify_card_unlock_session(user_id, card_id, x_card_unlock_token)
    return simulate_card_invoices(user_id, card_id, month_key, months, categoryId)



@router.post("/api/cards/{card_id}/purchase-simulation")
def simulate_card_purchase(
    card_id: int,
    payload: PurchaseSimulationPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    get_card_for_user(user_id, card_id)
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    base_month = month_key_from_date(purchase_date)
    installment_amounts = distribute_installments(payload.totalAmount, payload.totalInstallments)
    current_invoices = simulate_card_invoices(user_id, card_id, base_month, payload.months)
    simulated_by_month = {
        add_months(base_month, index): amount for index, amount in enumerate(installment_amounts)
    }

    projection = []
    for invoice in current_invoices:
        simulated_amount = round_money(simulated_by_month.get(invoice["month"], Decimal("0")))
        projection.append(
            {
                "month": invoice["month"],
                "currentInvoice": invoice["projected_total"],
                "simulatedInstallment": simulated_amount,
                "projectedTotal": round_money(invoice["projected_total"] + simulated_amount),
            }
        )

    return {
        "cardId": card_id,
        "totalAmount": round_money(payload.totalAmount),
        "totalInstallments": payload.totalInstallments,
        "installments": installment_amounts,
        "projection": projection,
    }



@router.get("/api/cards")
def cards(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    month_key = validate_month_text(month) or get_current_month()
    return get_cards_summary(current_user["id"], month_key)



@router.get("/api/cards-detail")
def cards_detail(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    validate_month_text(month) if month else None
    user_id = current_user["id"]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id, c.name, c.brand, c.last_four,
                   EXISTS (
                     SELECT 1
                     FROM card_pins p
                     WHERE p.card_id = c.id AND p.user_id = c.user_id
                   ) AS has_pin
            FROM cards c
            WHERE c.user_id = %s
            ORDER BY c.created_at ASC, c.id ASC
            """,
            (user_id,),
        )
        rows = normalize_rows(cursor.fetchall())

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "brand": row["brand"],
            "last_four": row["last_four"],
            "credit_limit": None,
            "invoice": None,
            "available_credit": None,
            "closing_day": None,
            "due_day": None,
            "has_pin": bool(row["has_pin"]),
            "is_unlocked": False,
        }
        for row in rows
    ]



@router.post("/api/cards")
def create_card(payload: CardPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    name = clean_text(payload.name, "Nome do cart\u00e3o", 100)
    brand = clean_text(payload.brand, "Bandeira", 40)
    last_four = clean_text(payload.lastFour, "Final do cart\u00e3o", 4)
    if not last_four.isdigit():
        raise HTTPException(status_code=400, detail="Final do cart\u00e3o deve conter 4 n\u00fameros.")
    color = validate_hex_color(payload.color)

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            INSERT INTO cards (user_id, name, brand, last_four, credit_limit, closing_day, due_day, color)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, name, brand, last_four, round_money(payload.creditLimit), payload.closingDay, payload.dueDay, color),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Cart\u00e3o n\u00e3o criado.")
    return row



@router.put("/api/cards/{card_id}")
def update_card(card_id: int, payload: CardUpdatePayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    current = get_card_for_user(user_id, card_id)
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    name = clean_text(payload.name, "Nome do cartão", 100) if "name" in fields_set and payload.name else current["name"]
    brand = clean_text(payload.brand, "Bandeira", 40) if "brand" in fields_set and payload.brand else current["brand"]
    last_four = clean_text(payload.lastFour, "Final do cartão", 4) if "lastFour" in fields_set and payload.lastFour else current["last_four"]
    if not str(last_four).isdigit():
        raise HTTPException(status_code=400, detail="Final do cartão deve conter 4 números.")
    credit_limit = round_money(
        payload.creditLimit if "creditLimit" in fields_set and payload.creditLimit is not None else current["credit_limit"]
    )
    closing_day = payload.closingDay if "closingDay" in fields_set and payload.closingDay else current["closing_day"]
    due_day = payload.dueDay if "dueDay" in fields_set and payload.dueDay else current["due_day"]
    color = validate_hex_color(payload.color) if "color" in fields_set and payload.color else current["color"]

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE cards
            SET name = %s,
                brand = %s,
                last_four = %s,
                credit_limit = %s,
                closing_day = %s,
                due_day = %s,
                color = %s
            WHERE user_id = %s AND id = %s
            RETURNING *
            """,
            (name, brand, last_four, credit_limit, closing_day, due_day, color, user_id, card_id),
        )
        return require_row(normalize_row(cursor.fetchone()), "Cartão não atualizado.")



@router.delete("/api/cards/{card_id}")
def delete_card(
    card_id: int,
    force: bool = False,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    get_card_for_user(user_id, card_id)
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS total FROM transactions WHERE user_id = %s AND card_id = %s",
            (user_id, card_id),
        )
        linked = int(require_row(normalize_row(cursor.fetchone()), "Cartão não encontrado.")["total"])
        if linked and not force:
            raise HTTPException(
                status_code=409,
                detail="Cartão possui lançamentos vinculados. Revise antes de excluir ou use force=true.",
            )
        if linked and force:
            cursor.execute(
                "UPDATE transactions SET card_id = NULL WHERE user_id = %s AND card_id = %s",
                (user_id, card_id),
            )
        cursor.execute("DELETE FROM card_pins WHERE user_id = %s AND card_id = %s", (user_id, card_id))
        cursor.execute("DELETE FROM cards WHERE user_id = %s AND id = %s", (user_id, card_id))
    invalidate_card_unlock_sessions(user_id, card_id)
    clear_card_pin_failures(user_id, card_id)
    audit_log("card_deleted", user_id, {"card_id": card_id, "linked_transactions": linked, "force": force})
    return {"deleted": True, "unlinkedTransactions": linked if force else 0}



@router.post("/api/cards/{card_id}/installments")
def create_installments(card_id: int, payload: InstallmentPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    title = clean_text(payload.title, "Descri\u00e7\u00e3o da compra", 200)
    notes = clean_text(payload.notes, "Observa\u00e7\u00f5es", 1000, required=False)
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    try:
        installment_amounts = distribute_installments(payload.totalAmount, payload.totalInstallments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT *
                FROM cards
                WHERE user_id = %s AND id = %s
                """,
                (user_id, card_id),
            )
            card = normalize_row(cursor.fetchone())
            if not card:
                raise HTTPException(status_code=404, detail="Cart\u00e3o n\u00e3o encontrado.")

            # Compra feita depois do fechamento entra na fatura do m\u00eas seguinte.
            first_month = first_billing_month(purchase_date, card.get("closing_day"))
            # UUID, n\u00e3o uma chave derivada de (user, cart\u00e3o, t\u00edtulo, data): duas
            # compras iguais no mesmo dia (duas passagens, dois notebooks da
            # fam\u00edlia) colidiam no mesmo grupo, duplicando installment_number e
            # fazendo a exclus\u00e3o de uma apagar as duas (DOM-01).
            group = str(uuid.uuid4())
            for number, amount in enumerate(installment_amounts, start=1):
                cursor.execute(
                    """
                    INSERT INTO transactions
                      (user_id, title, amount, type, category_id, payment_method, transaction_date, notes, card_id,
                       billing_month, installment_group, installment_number, total_installments, source)
                    VALUES (%s, %s, %s, 'expense', %s, 'cr\u00e9dito', %s, %s, %s, %s, %s, %s, %s, 'manual')
                    """,
                    (
                        user_id,
                        title,
                        amount,
                        payload.categoryId,
                        purchase_date,
                        notes,
                        card_id,
                        add_months(first_month, number - 1),
                        group,
                        number,
                        payload.totalInstallments,
                    ),
                )

            cursor.execute(
                """
                SELECT *
                FROM transactions
                WHERE user_id = %s AND installment_group = %s
                ORDER BY installment_number ASC
                """,
                (user_id, group),
            )
            rows = normalize_rows(cursor.fetchall())
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria ou cart\u00e3o inv\u00e1lido.") from None

    return {
        "createdInstallments": len(rows),
        "group": group,
        "rows": rows,
    }

