from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2 import errors

from app.api.deps import PlainDictRoute, get_current_user
from app.core.database import db_cursor
from app.core.logging import audit_log
from app.integrations.normalizer import build_duplicate_hash
from app.shared.dates import add_months, first_billing_month, get_current_month, month_key_from_date
from app.shared.money import apply_installment_interest, round_money
from app.shared.serialization import normalize_row, normalize_rows, require_row
from app.shared.validation import clean_text, validate_date_text, validate_month_text
from app.transactions.schemas import (
    InstallmentSimulationPayload,
    InstallmentWithoutCardPayload,
    RecurringPayload,
    TransactionPayload,
    TransactionUpdatePayload,
)
from app.transactions.service import get_recurring_suggestions, list_transactions, normalize_recurrence

router = APIRouter(route_class=PlainDictRoute)


@router.get("/api/transactions/suggestions")
def transaction_suggestions(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    month_key = validate_month_text(month) or get_current_month()
    return get_recurring_suggestions(current_user["id"], month_key)



@router.get("/api/transactions")
def transactions(
    month: str | None = None,
    type: Literal["income", "expense"] | None = None,
    categoryId: int | None = Query(default=None, ge=1),
    paymentMethod: str | None = None,
    source: Literal["manual", "csv_import", "open_finance_future"] | None = None,
    cardId: int | None = Query(default=None, ge=1),
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=250, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> dict:
    month_key = validate_month_text(month) if month else None
    payment_method = clean_text(paymentMethod, "Forma de pagamento", 50, required=False) if paymentMethod else None
    search_text = clean_text(search, "Busca", 120, required=False) if search else None
    items, has_more = list_transactions(
        current_user["id"],
        month_key,
        type,
        categoryId,
        payment_method,
        source,
        cardId,
        search_text,
        limit,
        offset,
    )
    return {"items": items, "hasMore": has_more}



@router.post("/api/transactions")
def create_transaction(payload: TransactionPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    title = clean_text(payload.title, "T\u00edtulo", 200)
    payment_method = clean_text(payload.paymentMethod, "Forma de pagamento", 50)
    notes = clean_text(payload.notes, "Observa\u00e7\u00f5es", 1000, required=False)
    transaction_date = validate_date_text(payload.transactionDate, "Data")
    billing_month = validate_month_text(payload.billingMonth)
    if payload.cardId and not billing_month:
        # DOM-03: sem isto, uma compra avulsa no cartão feita depois do
        # fechamento caía na fatura do mês da compra em vez da seguinte —
        # o mesmo cálculo que create_installments já faz para parceladas.
        # Card inexistente é deixado para a FK violation abaixo (mesmo
        # comportamento de erro que já existia); aqui só ajusta o mês
        # quando o cartão é encontrado.
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT closing_day FROM cards WHERE user_id = %s AND id = %s",
                (user_id, payload.cardId),
            )
            card_row = normalize_row(cursor.fetchone())
        if card_row:
            billing_month = first_billing_month(transaction_date, card_row.get("closing_day"))
    is_recurring, recurrence_type, recurrence_day = normalize_recurrence(
        payload.isRecurring,
        payload.recurrenceType,
        payload.recurrenceDay,
        transaction_date,
    )

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO transactions
                  (user_id, title, amount, type, category_id, payment_method, transaction_date, notes, card_id, billing_month,
                   installment_group, installment_number, total_installments, is_recurring, recurrence_type, recurrence_day, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, %s, %s, %s, 'manual')
                RETURNING *
                """,
                (
                    user_id,
                    title,
                    round_money(payload.amount),
                    payload.type,
                    payload.categoryId,
                    payment_method,
                    transaction_date,
                    notes,
                    payload.cardId,
                    billing_month,
                    is_recurring,
                    recurrence_type,
                    recurrence_day,
                ),
            )
            row = require_row(normalize_row(cursor.fetchone()), "Lan\u00e7amento n\u00e3o criado.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria ou cart\u00e3o inv\u00e1lido.") from None

    return row



@router.put("/api/transactions/{transaction_id}")
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM transactions
            WHERE user_id = %s AND id = %s
            """,
            (user_id, transaction_id),
        )
        current = normalize_row(cursor.fetchone())
    if not current:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")

    title = clean_text(payload.title, "Titulo", 200) if "title" in fields_set and payload.title else current["title"]
    amount = round_money(payload.amount if "amount" in fields_set and payload.amount is not None else current["amount"])
    transaction_type = payload.type if "type" in fields_set and payload.type else current["type"]
    category_id = payload.categoryId if "categoryId" in fields_set else current["category_id"]
    payment_method = (
        clean_text(payload.paymentMethod, "Forma de pagamento", 50)
        if "paymentMethod" in fields_set and payload.paymentMethod
        else current["payment_method"]
    )
    transaction_date = (
        validate_date_text(payload.transactionDate, "Data")
        if "transactionDate" in fields_set and payload.transactionDate
        else current["transaction_date"]
    )
    notes = (
        clean_text(payload.notes or "", "Observações", 1000, required=False)
        if "notes" in fields_set
        else current.get("notes", "")
    )
    card_id = payload.cardId if "cardId" in fields_set else current["card_id"]
    billing_month = validate_month_text(payload.billingMonth) if "billingMonth" in fields_set else current["billing_month"]

    if current["installment_group"]:
        # FIN-09: mudar type, valor ou mês de fatura de UMA parcela quebra a
        # integridade do grupo inteiro — a soma deixa de bater com o total
        # parcelado, e o mês de cobrança sai da sequência esperada. Título,
        # categoria, forma de pagamento e notas continuam livres.
        if transaction_type != current["type"]:
            raise HTTPException(status_code=409, detail="Não é possível alterar o tipo de uma parcela isoladamente.")
        if amount != round_money(current["amount"]):
            raise HTTPException(status_code=409, detail="Não é possível alterar o valor de uma parcela isoladamente.")
        if billing_month != current["billing_month"]:
            raise HTTPException(
                status_code=409, detail="Não é possível alterar o mês de fatura de uma parcela isoladamente."
            )

    # FIN-09: um lançamento importado editado (título, valor, data ou tipo)
    # deixava o duplicate_hash obsoleto — a próxima importação do mesmo
    # extrato reintroduziria a transação, já que o hash guardado não batia
    # mais com o conteúdo atual da linha.
    duplicate_hash = current.get("duplicate_hash")
    if current.get("source") == "csv_import":
        duplicate_hash = build_duplicate_hash(user_id, transaction_date, title, amount, transaction_type)

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                UPDATE transactions
                SET title = %s,
                    amount = %s,
                    type = %s,
                    category_id = %s,
                    payment_method = %s,
                    transaction_date = %s,
                    notes = %s,
                    card_id = %s,
                    billing_month = %s,
                    duplicate_hash = %s
                WHERE user_id = %s AND id = %s
                RETURNING *
                """,
                (
                    title,
                    amount,
                    transaction_type,
                    category_id,
                    payment_method,
                    transaction_date,
                    notes,
                    card_id,
                    billing_month,
                    duplicate_hash,
                    user_id,
                    transaction_id,
                ),
            )
            row = require_row(normalize_row(cursor.fetchone()), "Lançamento não atualizado.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria ou cartão inválido.") from None
    except errors.UniqueViolation:
        # O duplicate_hash recalculado (source = csv_import) pode colidir com
        # outra transação já existente se a edição a tornar idêntica a ela.
        raise HTTPException(
            status_code=409, detail="Já existe um lançamento idêntico (mesma data, descrição e valor)."
        ) from None

    return row



@router.post("/api/transactions/{transaction_id}/set-recurring")
def set_transaction_recurring(
    transaction_id: int,
    payload: RecurringPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    is_recurring, recurrence_type, recurrence_day = normalize_recurrence(
        payload.is_recurring,
        payload.recurrence_type,
        payload.recurrence_day,
    )

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE transactions
            SET is_recurring = %s, recurrence_type = %s, recurrence_day = %s
            WHERE user_id = %s AND id = %s
            RETURNING *
            """,
            (is_recurring, recurrence_type, recurrence_day, user_id, transaction_id),
        )
        row = normalize_row(cursor.fetchone())

    if not row:
        raise HTTPException(status_code=404, detail="Lan\u00e7amento n\u00e3o encontrado.")
    return row



@router.post("/api/installments/simulate")
def simulate_installments(
    payload: InstallmentSimulationPayload,
    _current_user: dict = Depends(get_current_user),
) -> dict:
    """Simula o impacto de uma compra parcelada sem exigir cartão cadastrado."""
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    base_month = month_key_from_date(purchase_date)
    
    # DOM-02: apply_installment_interest nunca arredonda a taxa para
    # centavos antes de aplicá-la (round_money(Decimal(rate)/100) fazia
    # 0,4% a.m. virar 0,00% e os juros sumirem).
    try:
        installment_amounts = apply_installment_interest(
            payload.totalAmount, payload.totalInstallments, payload.interestRate
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    
    simulated_by_month = {
        add_months(base_month, index): amount for index, amount in enumerate(installment_amounts)
    }
    
    projection = []
    for month_offset in range(payload.months):
        month_key = add_months(base_month, month_offset)
        simulated_amount = round_money(simulated_by_month.get(month_key, Decimal("0")))
        projection.append({
            "month": month_key,
            "simulatedInstallment": simulated_amount,
            "projectedTotal": simulated_amount,
        })
    
    return {
        "totalAmount": round_money(payload.totalAmount),
        "totalInstallments": payload.totalInstallments,
        "interestRate": payload.interestRate,
        "installments": [float(x) for x in installment_amounts],
        "projection": projection,
    }



@router.post("/api/installments")
def create_installments_without_card(
    payload: InstallmentWithoutCardPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Cria uma compra parcelada sem exigir cartão cadastrado."""
    user_id = current_user["id"]
    title = clean_text(payload.title, "Descrição da compra", 200)
    notes = clean_text(payload.notes, "Observações", 1000, required=False)
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    base_month = month_key_from_date(purchase_date)
    
    # DOM-02: apply_installment_interest nunca arredonda a taxa para
    # centavos antes de aplicá-la (round_money(Decimal(rate)/100) fazia
    # 0,4% a.m. virar 0,00% e os juros sumirem).
    try:
        installment_amounts = apply_installment_interest(
            payload.totalAmount, payload.totalInstallments, payload.interestRate
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    
    try:
        with db_cursor(commit=True) as cursor:
            # UUID pelo mesmo motivo de create_installments (DOM-01).
            group = str(uuid.uuid4())
            for number, amount in enumerate(installment_amounts, start=1):
                cursor.execute(
                    """
                    INSERT INTO transactions
                      (user_id, title, amount, type, category_id, payment_method, transaction_date, notes,
                       billing_month, installment_group, installment_number, total_installments, source)
                    VALUES (%s, %s, %s, 'expense', %s, 'compra parcelada', %s, %s, %s, %s, %s, %s, 'manual')
                    """,
                    (
                        user_id,
                        title,
                        amount,
                        payload.categoryId,
                        purchase_date,
                        notes,
                        add_months(base_month, number - 1),
                        group,
                        number,
                        payload.totalInstallments,
                    ),
                )
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria inválida.") from None
    
    return {
        "createdInstallments": payload.totalInstallments,
        "group": group,
        "totalAmount": round_money(payload.totalAmount),
    }



@router.get("/api/installments/future")
def get_future_installments(
    month: str = Query(..., min_length=7, max_length=7),
    limit: int = Query(default=24, ge=1, le=48),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Retorna parcelas futuras do usuário."""
    user_id = current_user["id"]
    
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT 
                t.id,
                t.installment_group,
                billing_month,
                title,
                COALESCE(c.name, 'Sem categoria') as category_name,
                amount,
                installment_number,
                total_installments
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s 
              AND t.type = 'expense'
              AND t.installment_group IS NOT NULL
              AND billing_month >= %s
            ORDER BY billing_month ASC, installment_number ASC
            LIMIT %s
            """,
            (user_id, month, limit),
        )
        rows = normalize_rows(cursor.fetchall())
    
    installments = []
    total_by_month = {}
    
    for row in rows:
        installments.append({
            "id": row["id"],
            "group": row["installment_group"],
            "month": row["billing_month"],
            "title": row["title"],
            "categoryName": row["category_name"],
            "amount": float(row["amount"]),
            "installmentNumber": row["installment_number"],
            "totalInstallments": row["total_installments"],
        })
        
        month_key = row["billing_month"]
        if month_key not in total_by_month:
            total_by_month[month_key] = Decimal("0")
        total_by_month[month_key] += Decimal(str(row["amount"]))
    
    # Calcular média mensal
    total_commitment = sum(total_by_month.values()) if total_by_month else Decimal("0")
    avg_monthly = round_money(total_commitment / len(total_by_month)) if total_by_month else Decimal("0")
    
    return {
        "installments": installments,
        "totalMonthlyCommitment": float(avg_monthly),
    }



@router.delete("/api/transactions/{transaction_id}")
def delete_transaction(
    transaction_id: int,
    scope: Literal["single", "group"] = "single",
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            SELECT *
            FROM transactions
            WHERE user_id = %s AND id = %s
            """,
            (user_id, transaction_id),
        )
        tx = normalize_row(cursor.fetchone())
        if not tx:
            raise HTTPException(status_code=404, detail="Lan\u00e7amento n\u00e3o encontrado.")

        if tx["installment_group"]:
            if scope != "group":
                # FIN-10: apagar uma parcela apagava o grupo inteiro sem
                # sinalizar isso antes \u2014 a API s\u00f3 avisava depois, com
                # deletedGroup: true. Agora \u00e9 preciso confirmar explicitamente.
                cursor.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM transactions
                    WHERE user_id = %s AND installment_group = %s
                    """,
                    (user_id, tx["installment_group"]),
                )
                total = int(
                    require_row(normalize_row(cursor.fetchone()), "Contagem de parcelas n\u00e3o encontrada.")["total"]
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Este lan\u00e7amento faz parte de um parcelamento com {total} parcela(s). "
                        "Envie scope=group para excluir todas."
                    ),
                )
            cursor.execute(
                """
            DELETE FROM transactions
            WHERE user_id = %s AND installment_group = %s
            """,
                (user_id, tx["installment_group"]),
            )
            audit_log("transaction_deleted", user_id, {"transaction_id": transaction_id, "group": True})
            return {"deletedGroup": True}

        cursor.execute(
            """
            DELETE FROM transactions
            WHERE user_id = %s AND id = %s
            """,
            (user_id, transaction_id),
        )
    audit_log("transaction_deleted", user_id, {"transaction_id": transaction_id, "group": False})
    return {"deleted": True}

