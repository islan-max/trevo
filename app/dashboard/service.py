from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.api.deps import request_cached
from app.budgets.service import get_budget_summary
from app.cards.service import get_cards_summary, get_invoice_totals_by_card, list_cards
from app.core.database import db_cursor
from app.goals.service import get_goals
from app.shared.dates import add_months, format_month_label
from app.shared.money import format_brl, round_money, to_decimal
from app.shared.serialization import normalize_row, normalize_rows, require_row
from app.users.service import get_effective_income, get_settings


def get_dashboard(user_id: str, month: str) -> dict:
    user_settings = get_settings(user_id)

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, month),
        )
        totals = require_row(normalize_row(cursor.fetchone()), "Totais do dashboard n\u00e3o encontrados.")

        cursor.execute(
            """
            SELECT c.name, c.color, COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.name, c.color
            HAVING COALESCE(SUM(t.amount), 0) > 0
            ORDER BY total DESC
            """,
            (user_id, month),
        )
        category_breakdown = normalize_rows(cursor.fetchall())

        # Os 12 meses da s\u00e9rie saem de uma \u00fanica agrega\u00e7\u00e3o; meses sem lan\u00e7amento
        # entram zerados no preenchimento abaixo.
        months = [add_months(month, idx - 11) for idx in range(12)]
        cursor.execute(
            """
            SELECT
              COALESCE(billing_month, substring(transaction_date from 1 for 7)) AS month_key,
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
            GROUP BY month_key
            """,
            (user_id, months),
        )
        trend_by_month = {row["month_key"]: row for row in normalize_rows(cursor.fetchall())}
        monthly_trend: list[dict] = []
        for month_key in months:
            row = trend_by_month.get(month_key)
            inflow = round_money(row["inflow"]) if row else Decimal("0.00")
            outflow = round_money(row["outflow"]) if row else Decimal("0.00")
            monthly_trend.append(
                {
                    "month": month_key,
                    "label": format_month_label(month_key),
                    "inflow": inflow,
                    "outflow": outflow,
                    "net": round_money(inflow - outflow),
                }
            )

        cursor.execute(
            """
            SELECT t.*, c.name AS category_name, c.color AS category_color, cards.name AS card_name
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            LEFT JOIN cards ON cards.id = t.card_id AND cards.user_id = t.user_id
            WHERE t.user_id = %s
            ORDER BY t.transaction_date DESC, t.id DESC
            LIMIT 12
            """,
            (user_id,),
        )
        recent_transactions = normalize_rows(cursor.fetchall())

        cursor.execute(
            """
            SELECT payment_method, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY payment_method
            ORDER BY total DESC
            """,
            (user_id, month),
        )
        payment_method_breakdown = normalize_rows(cursor.fetchall())

        previous_month = add_months(month, -1)
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, previous_month),
        )
        previous_totals = require_row(normalize_row(cursor.fetchone()), "Totais do m\u00eas anterior n\u00e3o encontrados.")

    inflow = round_money(totals["inflow"])
    outflow = round_money(totals["outflow"])
    base_income = round_money(user_settings["monthly_income"] or 0)
    reserve_amount = round_money(user_settings.get("reserve_amount") or 0)
    # DOM-05: balance e o percentual comprometido usam a renda EFETIVA (o
    # maior entre configurada e o que já entrou), não a soma das duas — ver
    # get_effective_income.
    effective_income = get_effective_income(user_settings, inflow)
    balance = round_money(effective_income - outflow)
    goals = get_goals(user_id, month)
    previous_inflow = round_money(previous_totals["inflow"])
    previous_outflow = round_money(previous_totals["outflow"])
    previous_balance = round_money(get_effective_income(user_settings, previous_inflow) - previous_outflow)
    salary_base = effective_income
    committed_percent = (
        int(((outflow + reserve_amount) / salary_base * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
        if salary_base > 0
        else 0
    )

    return {
        "month": month,
        "monthlyIncome": base_income,
        "salaryBase": base_income,
        "extraIncome": inflow,
        "inflow": inflow,
        "outflow": outflow,
        "balance": balance,
        "projectedBalance": balance,
        "salaryCommittedPercent": committed_percent,
        "availableToday": round_money(goals["allowedRemaining"] / Decimal(max(goals["totalDays"] - goals["progressDay"] + 1, 1))),
        "rhythmStatus": goals["goalStatus"],
        "closingProjection": goals["projectedClosing"],
        "reserve": {
            "monthlyPlanned": reserve_amount,
            "goalAmount": round_money(user_settings.get("reserve_goal_amount") or 0),
            "currentAmount": round_money(user_settings.get("reserve_current_amount") or 0),
        },
        "previousMonthComparison": {
            "month": previous_month,
            "inflow": previous_inflow,
            "outflow": previous_outflow,
            "balance": previous_balance,
            "balanceDelta": round_money(balance - previous_balance),
            "outflowDelta": round_money(outflow - previous_outflow),
        },
        "categoryBreakdown": category_breakdown,
        "paymentMethodBreakdown": payment_method_breakdown,
        "cardInvoices": get_cards_summary(user_id, month),
        "monthlyTrend": monthly_trend,
        "recentTransactions": recent_transactions,
    }



def get_month_totals(user_id: str, month: str) -> dict:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, month),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Totais do m\u00eas n\u00e3o encontrados.")
    return {"inflow": round_money(row["inflow"]), "outflow": round_money(row["outflow"])}



def get_score_label(score: int) -> dict:
    if score <= 300:
        return {"label": "Cr\u00edtico", "color": "#eb4d43"}
    if score <= 500:
        return {"label": "Regular", "color": "#ff9800"}
    if score <= 700:
        return {"label": "Razo\u00e1vel", "color": "#ffd54f"}
    if score <= 850:
        return {"label": "Bom", "color": "#9be768"}
    return {"label": "Excelente", "color": "#2f7d32"}



def calculate_score(user_id: str, month: str) -> dict:
    return request_cached(("score", user_id, month), lambda: _compute_score(user_id, month))



def _compute_score(user_id: str, month: str) -> dict:
    user_settings = get_settings(user_id)
    monthly_income = round_money(user_settings["monthly_income"] or 0)
    totals = get_month_totals(user_id, month)
    inflow = totals["inflow"]
    outflow = totals["outflow"]
    base = 1000
    breakdown = {"gastos": 0, "consistência": 0, "reservas": 0, "cartões": 0, "orçamento": 0}

    # DOM-05: renda efetiva (o maior entre configurada e o que já entrou),
    # não a soma das duas — ver get_effective_income.
    denominator = get_effective_income(user_settings, inflow)
    ratio_gastos = (outflow / denominator) if denominator > 0 else (Decimal("1") if outflow > 0 else Decimal("0"))
    if ratio_gastos > Decimal("0.9"):
        breakdown["gastos"] = -200
    elif ratio_gastos > Decimal("0.75"):
        breakdown["gastos"] = -120
    elif ratio_gastos > Decimal("0.6"):
        breakdown["gastos"] = -60
    elif ratio_gastos > Decimal("0.4"):
        breakdown["gastos"] = -20

    recent_months = [add_months(month, offset) for offset in (-2, -1, 0)]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
            """,
            (user_id, recent_months),
        )
        consistency_row = require_row(normalize_row(cursor.fetchone()), "Consist\u00eancia n\u00e3o encontrada.")
        total_recent = int(consistency_row["total"])
        if total_recent >= 20:
            breakdown["consistência"] = 50
        elif total_recent >= 10:
            breakdown["consistência"] = 25

        cursor.execute(
            """
            SELECT COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND c.role IN ('reserve', 'investment')
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            """,
            (user_id, month),
        )
        reserve_row = require_row(normalize_row(cursor.fetchone()), "Reservas n\u00e3o encontradas.")

    total_reserva = round_money(reserve_row["total"] or 0)
    if total_reserva > 0 and monthly_income > 0:
        breakdown["reservas"] = min(100, int((total_reserva / monthly_income) * Decimal("200")))

    invoice_totals = get_invoice_totals_by_card(user_id, month)
    for card in list_cards(user_id):
        credit_limit = round_money(card["credit_limit"] or 0)
        if credit_limit <= 0:
            continue
        uso_pct = invoice_totals.get(int(card["id"]), Decimal("0")) / credit_limit
        if uso_pct > Decimal("0.9"):
            breakdown["cartões"] -= 80
        elif uso_pct > Decimal("0.7"):
            breakdown["cartões"] -= 40

    budget = get_budget_summary(user_id, month)
    over_budget = len([item for item in budget["items"] if item["status"] == "over"])
    attention_budget = len([item for item in budget["items"] if item["status"] == "attention"])
    if over_budget:
        breakdown["orçamento"] -= min(120, over_budget * 40)
    elif budget["items"] and not attention_budget:
        breakdown["orçamento"] += 50

    base += sum(breakdown.values())
    score = max(0, min(1000, int(base)))
    label = get_score_label(score)
    return {"score": score, "label": label["label"], "color": label["color"], "breakdown": breakdown}



def get_alerts_for_month(user_id: str, month: str) -> list[dict]:
    user_settings = get_settings(user_id)
    totals = get_month_totals(user_id, month)
    alerts: list[dict] = []

    invoice_totals = get_invoice_totals_by_card(user_id, month)
    for card in list_cards(user_id):
        credit_limit = round_money(card["credit_limit"] or 0)
        if credit_limit <= 0:
            continue
        invoice = invoice_totals.get(int(card["id"]), Decimal("0"))
        usage = invoice / credit_limit
        if usage > Decimal("0.8"):
            usage_percent = int((usage * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
            alerts.append(
                {
                    "type": "danger",
                    "category": "cart\u00e3o",
                    "message": f"Cart\u00e3o {card['name']} est\u00e1 com {usage_percent}% do limite",
                }
            )

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id, c.name, COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.id, c.name
            HAVING COALESCE(SUM(t.amount), 0) > 0
            """,
            (user_id, month),
        )
        current_categories = normalize_rows(cursor.fetchall())
        previous_months = [add_months(month, offset) for offset in (-3, -2, -1)]
        # M\u00e9dia dos 3 meses anteriores de todas as categorias em uma query s\u00f3,
        # em vez de uma por categoria.
        category_ids = [int(category["id"]) for category in current_categories]
        previous_by_category: dict[int, Decimal] = {}
        if category_ids:
            cursor.execute(
                """
                SELECT category_id, COALESCE(SUM(amount), 0) AS total
                FROM transactions
                WHERE user_id = %s
                  AND type = 'expense'
                  AND category_id = ANY(%s)
                  AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
                GROUP BY category_id
                """,
                (user_id, category_ids, previous_months),
            )
            previous_by_category = {
                int(row["category_id"]): to_decimal(row["total"] or 0)
                for row in normalize_rows(cursor.fetchall())
            }

        for category in current_categories:
            previous_total = previous_by_category.get(int(category["id"]), Decimal("0"))
            average = round_money(previous_total / Decimal("3"))
            current_total = round_money(category["total"] or 0)
            if average > 0 and current_total > average * Decimal("1.3"):
                percent = int((((current_total / average) - 1) * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
                alerts.append(
                    {
                        "type": "warning",
                        "category": "gastos",
                        "message": f"Gastos com {category['name']} {percent}% acima da m\u00e9dia",
                    }
                )

        next_month = add_months(month, 1)
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month = %s
            """,
            (user_id, next_month),
        )
        next_invoice_row = require_row(normalize_row(cursor.fetchone()), "Fatura estimada n\u00e3o encontrada.")

    if totals["inflow"] <= 0:
        alerts.append(
            {
                "type": "warning",
                "category": "gastos",
                "message": f"Nenhuma entrada lan\u00e7ada para {format_month_label(month)}",
            }
        )

    # DOM-05: renda efetiva (o maior entre configurada e o que já entrou),
    # não a soma das duas — ver get_effective_income.
    projected_balance = get_effective_income(user_settings, totals["inflow"]) - totals["outflow"]
    if projected_balance < 0:
        alerts.append(
            {
                "type": "danger",
                "category": "gastos",
                "message": f"Saldo projetado negativo em {format_brl(abs(projected_balance))}",
            }
        )

    next_invoice = round_money(next_invoice_row["total"] or 0)
    if next_invoice > Decimal("500"):
        alerts.append(
            {
                "type": "info",
                "category": "cart\u00e3o",
                "message": f"Fatura estimada em {format_brl(next_invoice)} para o pr\u00f3ximo m\u00eas",
            }
        )

    goals = get_goals(user_id, month)
    days = goals["days"]
    goal_reference = to_decimal(goals.get("targetDailyGoal") or goals["dailyGoal"])
    exceeded_days = [day for day in days if to_decimal(day["spent"]) > goal_reference]
    if days and len(exceeded_days) / len(days) > 0.5:
        alerts.append(
            {
                "type": "warning",
                "category": "meta",
                "message": "Meta di\u00e1ria estourada em mais da metade dos dias",
            }
        )
    if goals.get("goalStatus") in {"yellow", "red"}:
        alerts.append(
            {
                "type": "warning" if goals["goalStatus"] == "yellow" else "danger",
                "category": "meta",
                "message": goals["riskAlert"],
            }
        )

    budget = get_budget_summary(user_id, month)
    for item in budget["items"]:
        if item["status"] == "over":
            alerts.append(
                {
                    "type": "danger",
                    "category": "orcamento",
                    "message": f"{item['categoryName']} passou do orçamento em {format_brl(abs(item['remaining']))}",
                }
            )
        elif item["status"] == "attention":
            alerts.append(
                {
                    "type": "warning",
                    "category": "orcamento",
                    "message": f"{item['categoryName']} está perto do limite planejado.",
                }
            )

    return alerts

