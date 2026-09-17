from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException

from app.api.deps import request_cached
from app.core.database import db_cursor, storage_available
from app.core.ephemeral import card_pin_failures, card_unlock_sessions
from app.core.security import token_hash
from app.shared.dates import add_months
from app.shared.money import round_money
from app.shared.serialization import normalize_row, normalize_rows, require_row

CARD_UNLOCK_SECONDS = 15 * 60
PIN_FAILURE_WINDOW_SECONDS = 5 * 60
PIN_MAX_ATTEMPTS = 3


def list_cards(user_id: str) -> list[dict]:
    return request_cached(("cards", user_id), lambda: _compute_cards(user_id))



def _compute_cards(user_id: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM cards
            WHERE user_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (user_id,),
        )
        return normalize_rows(cursor.fetchall())



def get_cards_summary(user_id: str, month: str) -> list[dict]:
    # PERF-03: bootstrap() e get_reports_summary() chamam get_dashboard()
    # (que j\u00e1 pede isto por dentro) E get_cards_summary() de novo com os
    # mesmos argumentos \u2014 cache de escopo de request evita computar duas
    # vezes dentro do mesmo request, sem risco de servir dado velho entre
    # requests diferentes.
    return request_cached(("cards_summary", user_id, month), lambda: _compute_cards_summary(user_id, month))



def _compute_cards_summary(user_id: str, month: str) -> list[dict]:
    """Resumo de todos os cart\u00f5es do usu\u00e1rio para o m\u00eas.

    PERF-02: a vers\u00e3o anterior fazia 2 queries por cart\u00e3o + 1 por grupo de
    parcelamento (at\u00e9 ~70 idas ao banco com 3 cart\u00f5es e 20 grupos), cada uma
    abrindo a pr\u00f3pria conex\u00e3o em serverless. Agora s\u00e3o sempre 4 queries no
    total, batidas por card_id \u2014 n\u00e3o escala com o n\u00famero de cart\u00f5es/grupos.
    """
    cards = list_cards(user_id)
    if not cards:
        return []
    card_ids = [int(card["id"]) for card in cards]

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT card_id, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND card_id = ANY(%s)
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY card_id
            """,
            (user_id, card_ids, month),
        )
        invoice_by_card = {row["card_id"]: round_money(row["total"]) for row in normalize_rows(cursor.fetchall())}

        # Parcela corrente de cada grupo (existe uma linha com billing_month
        # = m\u00eas pedido).
        cursor.execute(
            """
            SELECT card_id, installment_group, installment_number, total_installments, title, amount
            FROM transactions
            WHERE user_id = %s
              AND card_id = ANY(%s)
              AND installment_group IS NOT NULL
              AND billing_month = %s
            """,
            (user_id, card_ids, month),
        )
        current_by_group = {
            (row["card_id"], row["installment_group"]): row for row in normalize_rows(cursor.fetchall())
        }

        # Para grupos sem parcela no m\u00eas corrente: a mais antiga entre as
        # faturas futuras, mais quantas restam. DISTINCT ON pega a linha de
        # billing_month mais cedo por grupo; a janela conta todo o grupo.
        cursor.execute(
            """
            SELECT DISTINCT ON (card_id, installment_group)
              card_id, installment_group, title, amount,
              COUNT(*) OVER (PARTITION BY card_id, installment_group) AS future_count
            FROM transactions
            WHERE user_id = %s
              AND card_id = ANY(%s)
              AND installment_group IS NOT NULL
              AND billing_month >= %s
            ORDER BY card_id, installment_group, billing_month ASC
            """,
            (user_id, card_ids, month),
        )
        future_by_group = {
            (row["card_id"], row["installment_group"]): row for row in normalize_rows(cursor.fetchall())
        }

        cursor.execute(
            """
            SELECT card_id, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS remaining_installments
            FROM transactions
            WHERE user_id = %s
              AND card_id = ANY(%s)
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month >= %s
            GROUP BY card_id
            """,
            (user_id, card_ids, month),
        )
        commitment_by_card = {
            row["card_id"]: {
                "committedLimit": round_money(row["total"]),
                "remainingInstallments": int(row["remaining_installments"]),
            }
            for row in normalize_rows(cursor.fetchall())
        }

    groups_by_card: dict[int, set[str]] = {}
    for card_id, group in current_by_group:
        groups_by_card.setdefault(card_id, set()).add(group)
    for card_id, group in future_by_group:
        groups_by_card.setdefault(card_id, set()).add(group)

    result: list[dict] = []
    for card in cards:
        # list_cards() agora é cacheado por request (request_cached) — os
        # dicts aqui são compartilhados com quem mais chamar list_cards()
        # nesta mesma request, então os campos calculados abaixo vão numa
        # cópia, nunca no dict original.
        card = dict(card)
        card_id = int(card["id"])
        invoice = invoice_by_card.get(card_id, Decimal("0"))

        active_installments: list[dict] = []
        for group in sorted(groups_by_card.get(card_id, ())):
            key = (card_id, group)
            current_row = current_by_group.get(key)
            if current_row:
                active_installments.append(
                    {
                        "title": current_row["title"],
                        "installmentLabel": f'{current_row["installment_number"]}/{current_row["total_installments"]}',
                        "remaining": current_row["total_installments"] - current_row["installment_number"],
                        "amount": round_money(current_row["amount"]),
                    }
                )
                continue
            future_row = future_by_group.get(key)
            if future_row:
                active_installments.append(
                    {
                        "title": future_row["title"],
                        "installmentLabel": "\u00c0 frente",
                        "remaining": int(future_row["future_count"]),
                        "amount": round_money(future_row["amount"]),
                    }
                )

        card["invoice"] = invoice
        card["availableCredit"] = round_money(card["credit_limit"] - invoice)
        commitment = commitment_by_card.get(card_id, {"committedLimit": Decimal("0"), "remainingInstallments": 0})
        card["committedLimit"] = commitment["committedLimit"]
        card["remainingInstallments"] = commitment["remainingInstallments"]
        usage = (invoice / round_money(card["credit_limit"])) if round_money(card["credit_limit"]) > 0 else Decimal("0")
        card["invoiceAlert"] = usage > Decimal("0.8")
        card["activeInstallmentsCount"] = len(active_installments)
        card["activeInstallments"] = active_installments
        result.append(card)

    return result



def get_card_for_user(user_id: str, card_id: int) -> dict:
    with db_cursor() as cursor:
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
    return card



def get_card_pin_row(user_id: str, card_id: int) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, card_id, user_id, pin_hash, created_at
            FROM card_pins
            WHERE user_id = %s AND card_id = %s
            """,
            (user_id, card_id),
        )
        return normalize_row(cursor.fetchone())



def get_invoice_totals_by_card(user_id: str, month: str) -> dict[int, Decimal]:
    """Fatura de todos os cartões do mês numa query só.

    calculate_score e get_alerts_for_month somavam a fatura cartão a cartão
    via get_invoice_total (uma query cada) — com N cartões, N queries em cada
    função. Aqui é sempre 1 query batida por card_id, igual ao padrão já
    usado em _compute_cards_summary.
    """
    return request_cached(
        ("invoice_totals_by_card", user_id, month), lambda: _compute_invoice_totals_by_card(user_id, month)
    )



def _compute_invoice_totals_by_card(user_id: str, month: str) -> dict[int, Decimal]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT card_id, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND card_id IS NOT NULL
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY card_id
            """,
            (user_id, month),
        )
        rows = normalize_rows(cursor.fetchall())
    return {int(row["card_id"]): round_money(row["total"]) for row in rows}



def get_invoice_total(user_id: str, card_id: int, month: str) -> Decimal:
    return request_cached(("invoice_total", user_id, card_id, month), lambda: _compute_invoice_total(user_id, card_id, month))



def _compute_invoice_total(user_id: str, card_id: int, month: str) -> Decimal:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND card_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, card_id, month),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Fatura n\u00e3o encontrada.")
    return round_money(row["total"])



def get_active_installments(user_id: str, card_id: int, month: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT title, amount, billing_month, installment_number, total_installments
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND installment_group IS NOT NULL
              AND billing_month = %s
            ORDER BY transaction_date DESC, id DESC
            """,
            (user_id, card_id, month),
        )
        rows = normalize_rows(cursor.fetchall())

    installments: list[dict] = []
    for row in rows:
        total = int(row["total_installments"] or 0)
        current = int(row["installment_number"] or 0)
        remaining = max(total - current, 0) if total else 0
        progress = round((current / total) * 100, 2) if total else 0
        installments.append(
            {
                "title": row["title"],
                "amount": round_money(row["amount"]),
                "billing_month": row["billing_month"],
                "installment_number": current,
                "total_installments": total,
                "installment_label": f"{current}/{total}" if total else "-",
                "remaining": remaining,
                "progress": progress,
            }
        )
    return installments



def simulate_card_invoices(
    user_id: str,
    card_id: int,
    start_month: str,
    months: int,
    category_id: int | None = None,
) -> list[dict]:
    """PERF-06: uma query com GROUP BY para todos os meses simulados, em vez
    de uma query por m\u00eas (at\u00e9 24 idas ao banco em simula\u00e7\u00f5es mais longas)."""
    month_keys = [add_months(start_month, offset) for offset in range(months)]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(billing_month, substring(transaction_date from 1 for 7)) AS month_key,
                   COALESCE(SUM(amount), 0) AS total, COUNT(*) AS installments_count
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND type = 'expense'
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
              AND (%s IS NULL OR category_id = %s)
            GROUP BY month_key
            """,
            (user_id, card_id, month_keys, category_id, category_id),
        )
        totals_by_month = {row["month_key"]: row for row in normalize_rows(cursor.fetchall())}

    result: list[dict] = []
    for month_key in month_keys:
        row = totals_by_month.get(month_key)
        total = round_money(row["total"]) if row else Decimal("0")
        count = int(row["installments_count"]) if row else 0
        result.append(
            {
                "month": month_key,
                "projected_total": total,
                "projectedTotal": total,
                "installments_count": count,
                "itemsCount": count,
            }
        )
    return result



def get_card_commitment(user_id: str, card_id: int, month: str, category_id: int | None = None) -> dict:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS remaining_installments
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month >= %s
              AND (%s IS NULL OR category_id = %s)
            """,
            (user_id, card_id, month, category_id, category_id),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Comprometimento do cartão não encontrado.")
    return {
        "committedLimit": round_money(row["total"]),
        "remainingInstallments": int(row["remaining_installments"]),
    }



def get_grouped_installment_purchases(user_id: str, card_id: int, month: str, category_id: int | None = None) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              installment_group,
              MIN(title) AS title,
              MIN(transaction_date) AS purchase_date,
              MIN(billing_month) AS first_open_month,
              MAX(billing_month) AS last_month,
              MAX(total_installments) AS total_installments,
              COUNT(*) AS remaining_installments,
              COALESCE(SUM(amount), 0) AS remaining_amount
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month >= %s
              AND (%s IS NULL OR category_id = %s)
            GROUP BY installment_group
            ORDER BY first_open_month ASC, title ASC
            """,
            (user_id, card_id, month, category_id, category_id),
        )
        rows = normalize_rows(cursor.fetchall())

    return [
        {
            "group": row["installment_group"],
            "title": row["title"],
            "purchaseDate": row["purchase_date"],
            "firstOpenMonth": row["first_open_month"],
            "lastMonth": row["last_month"],
            "totalInstallments": int(row["total_installments"] or 0),
            "remainingInstallments": int(row["remaining_installments"] or 0),
            "remainingAmount": round_money(row["remaining_amount"]),
        }
        for row in rows
    ]



def get_recent_card_transactions(user_id: str, card_id: int) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT t.id, t.title, t.amount, t.type, t.payment_method, t.transaction_date, t.notes,
                   t.billing_month, t.installment_number, t.total_installments, t.created_at,
                   c.name AS category_name, c.color AS category_color
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s AND t.card_id = %s
            ORDER BY t.transaction_date DESC, t.id DESC
            LIMIT 20
            """,
            (user_id, card_id),
        )
        return normalize_rows(cursor.fetchall())



def get_unlocked_card_details(
    user_id: str,
    card_id: int,
    month: str,
    include_token: bool = False,
    category_id: int | None = None,
) -> dict:
    card = get_card_for_user(user_id, card_id)
    invoice = get_invoice_total(user_id, card_id, month)
    commitment = get_card_commitment(user_id, card_id, month, category_id)
    usage = (invoice / round_money(card["credit_limit"])) if round_money(card["credit_limit"]) > 0 else Decimal("0")
    invoice_alert = None
    if usage > Decimal("0.8"):
        invoice_alert = {
            "type": "danger" if usage > Decimal("0.9") else "warning",
            "message": "Fatura alta para o limite do cartão.",
            "usagePercent": int((usage * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP)),
        }
    details = {
        "id": card["id"],
        "name": card["name"],
        "brand": card["brand"],
        "last_four": card["last_four"],
        "credit_limit": round_money(card["credit_limit"]),
        "invoice": invoice,
        "available_credit": round_money(card["credit_limit"] - invoice),
        "committed_limit": commitment["committedLimit"],
        "committedLimit": commitment["committedLimit"],
        "remainingInstallments": commitment["remainingInstallments"],
        "closing_day": card["closing_day"],
        "due_day": card["due_day"],
        "active_installments": get_active_installments(user_id, card_id, month),
        "groupedInstallments": get_grouped_installment_purchases(user_id, card_id, month, category_id),
        "invoiceAlert": invoice_alert,
        "upcoming_invoices": simulate_card_invoices(user_id, card_id, month, 12, category_id),
        "recent_transactions": get_recent_card_transactions(user_id, card_id),
        "is_unlocked": True,
    }
    if include_token:
        token, expires_at = create_card_unlock_session(user_id, card_id)
        details["unlock_token"] = token
        details["unlock_expires_at"] = expires_at.isoformat()
    return details



def card_pin_failure_key(user_id: str, card_id: int) -> str:
    return f"{user_id}:{card_id}"



def enforce_card_pin_rate_limit(user_id: str, card_id: int) -> None:
    key = card_pin_failure_key(user_id, card_id)
    now = datetime.now(UTC).timestamp()
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT attempts, first_attempt_at, blocked_until
                FROM card_pin_failures_state
                WHERE user_id = %s AND card_id = %s
                """,
                (user_id, card_id),
            )
            row = normalize_row(cursor.fetchone())
            if not row:
                return

            blocked_until = row.get("blocked_until")
            if isinstance(blocked_until, datetime) and blocked_until > datetime.now(UTC):
                raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em 5 minutos.")

            first_attempt = row.get("first_attempt_at")
            if isinstance(first_attempt, datetime) and (
                datetime.now(UTC) - first_attempt
            ).total_seconds() > PIN_FAILURE_WINDOW_SECONDS:
                cursor.execute(
                    "DELETE FROM card_pin_failures_state WHERE user_id = %s AND card_id = %s",
                    (user_id, card_id),
                )
        return

    entry = card_pin_failures.get(key)
    if not entry:
        return

    blocked_until = float(entry.get("blocked_until") or 0)
    if blocked_until > now:
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em 5 minutos.")

    first_attempt = float(entry.get("first_attempt") or 0)
    if now - first_attempt > PIN_FAILURE_WINDOW_SECONDS:
        card_pin_failures.pop(key, None)



def record_card_pin_failure(user_id: str, card_id: int) -> int:
    key = card_pin_failure_key(user_id, card_id)
    now = datetime.now(UTC).timestamp()
    if storage_available():
        now_dt = datetime.now(UTC)
        blocked_until_dt = None
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT attempts, first_attempt_at
                FROM card_pin_failures_state
                WHERE user_id = %s AND card_id = %s
                """,
                (user_id, card_id),
            )
            row = normalize_row(cursor.fetchone())
            if not row or (
                isinstance(row.get("first_attempt_at"), datetime)
                and (now_dt - row["first_attempt_at"]).total_seconds() > PIN_FAILURE_WINDOW_SECONDS
            ):
                attempts = 1
                first_attempt_at = now_dt
            else:
                attempts = int(row["attempts"]) + 1
                first_attempt_at = row["first_attempt_at"]

            attempts_remaining = max(PIN_MAX_ATTEMPTS - attempts, 0)
            if attempts >= PIN_MAX_ATTEMPTS:
                blocked_until_dt = now_dt + timedelta(seconds=PIN_FAILURE_WINDOW_SECONDS)

            cursor.execute(
                """
                INSERT INTO card_pin_failures_state
                  (user_id, card_id, attempts, first_attempt_at, blocked_until)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, card_id)
                DO UPDATE SET
                  attempts = EXCLUDED.attempts,
                  first_attempt_at = EXCLUDED.first_attempt_at,
                  blocked_until = EXCLUDED.blocked_until
                """,
                (user_id, card_id, attempts, first_attempt_at, blocked_until_dt),
            )
        return attempts_remaining

    entry = card_pin_failures.get(key)
    if not entry or now - float(entry.get("first_attempt") or 0) > PIN_FAILURE_WINDOW_SECONDS:
        entry = {"count": 0, "first_attempt": now, "blocked_until": 0}

    entry["count"] = int(entry["count"]) + 1
    attempts_remaining = max(PIN_MAX_ATTEMPTS - int(entry["count"]), 0)
    if int(entry["count"]) >= PIN_MAX_ATTEMPTS:
        entry["blocked_until"] = now + PIN_FAILURE_WINDOW_SECONDS
    card_pin_failures[key] = entry
    return attempts_remaining



def clear_card_pin_failures(user_id: str, card_id: int) -> None:
    card_pin_failures.pop(card_pin_failure_key(user_id, card_id), None)
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM card_pin_failures_state WHERE user_id = %s AND card_id = %s",
                (user_id, card_id),
            )



def invalidate_card_unlock_sessions(user_id: str, card_id: int) -> None:
    for token, session in list(card_unlock_sessions.items()):
        if session["user_id"] == user_id and int(session["card_id"]) == card_id:
            card_unlock_sessions.pop(token, None)
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM card_unlock_sessions_state WHERE user_id = %s AND card_id = %s",
                (user_id, card_id),
            )



def create_card_unlock_session(user_id: str, card_id: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=CARD_UNLOCK_SECONDS)
    card_unlock_sessions[token] = {
        "user_id": user_id,
        "card_id": card_id,
        "expires_at": expires_at,
    }
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO card_unlock_sessions_state (token_hash, user_id, card_id, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (token_hash(token), user_id, card_id, expires_at),
            )
    return token, expires_at



def verify_card_unlock_session(user_id: str, card_id: int, token: str) -> None:
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT user_id, card_id, expires_at
                FROM card_unlock_sessions_state
                WHERE token_hash = %s
                """,
                (token_hash(token),),
            )
            row = normalize_row(cursor.fetchone())
            if not row:
                raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")
            expires_at = row["expires_at"]
            if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
                cursor.execute("DELETE FROM card_unlock_sessions_state WHERE token_hash = %s", (token_hash(token),))
                raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")
            if str(row["user_id"]) != user_id or int(row["card_id"]) != card_id:
                raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o inv\u00e1lido.")
        return

    session = card_unlock_sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")

    expires_at = session["expires_at"]
    if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
        card_unlock_sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")

    if session["user_id"] != user_id or int(session["card_id"]) != card_id:
        raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o inv\u00e1lido.")

