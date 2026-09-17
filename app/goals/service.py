from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.api.deps import request_cached
from app.core.database import db_cursor
from app.shared import clock
from app.shared.money import round_money, to_decimal
from app.shared.serialization import normalize_row, normalize_rows, require_row
from app.users.service import get_effective_income, get_settings


def get_goals(user_id: str, month: str) -> dict:
    return request_cached(("goals", user_id, month), lambda: _compute_goals(user_id, month))



def _compute_goals(user_id: str, month: str) -> dict:
    user_settings = get_settings(user_id)
    year, month_num = [int(part) for part in month.split("-")]

    from calendar import monthrange

    total_days = monthrange(year, month_num)[1]
    # DOM-04: "hoje" e "mês atual" do ponto de vista do usuário, não de UTC —
    # ver app/shared/clock.py.
    today = clock.today()
    current_month = today.strftime("%Y-%m")
    if month < current_month:
        progress_day = total_days
    elif month > current_month:
        progress_day = 1
    else:
        progress_day = min(today.day, total_days)
    cutoff_date = date(year, month_num, progress_day).isoformat()

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              substring(transaction_date from 9 for 2) AS day,
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) AS income,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY day
            """,
            (user_id, month),
        )
        rows = normalize_rows(cursor.fetchall())
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
        totals = require_row(normalize_row(cursor.fetchone()), "Totais das metas não encontrados.")
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
              AND transaction_date <= %s
            """,
            (user_id, month, cutoff_date),
        )
        current_outflow_row = require_row(normalize_row(cursor.fetchone()), "Gasto atual não encontrado.")

    # FIN-02: uma parcela ou compra pós-fechamento pode ter transaction_date
    # em um mês diferente do billing_month (a fatura em que ela cai). O dia de
    # origem às vezes nem existe no mês exibido (dia 31 comprado em agosto,
    # faturado em setembro, que tem 30 dias) — concentra no último dia em vez
    # de descartar, para o somatório do calendário nunca divergir do total do
    # mês exibido em outro lugar da tela.
    day_map: dict[int, dict[str, Decimal]] = {}
    for row in rows:
        day_number = min(int(row["day"]), total_days)
        bucket = day_map.setdefault(day_number, {"income": Decimal("0"), "expense": Decimal("0")})
        bucket["income"] = round_money(bucket["income"] + round_money(row["income"]))
        bucket["expense"] = round_money(bucket["expense"] + round_money(row["expense"]))
    days: list[dict] = []
    legacy_daily_goal = round_money(user_settings["daily_goal"])
    reserve_amount = round_money(user_settings.get("reserve_amount") or 0)
    inflow = round_money(totals["inflow"])
    outflow = round_money(totals["outflow"])
    outflow_to_today = round_money(current_outflow_row["outflow"])
    # DOM-05: renda efetiva (o maior entre configurada e o que já entrou),
    # não a soma das duas — ver get_effective_income.
    available_budget = round_money(get_effective_income(user_settings, inflow) - reserve_amount)
    recommended_daily_goal = round_money(available_budget / Decimal(total_days)) if available_budget > 0 else Decimal("0.00")
    target_daily_goal = legacy_daily_goal if legacy_daily_goal > 0 else recommended_daily_goal
    # A média fica sem arredondar para projetar; arredondar antes de multiplicar
    # pelos dias do mês espalhava o erro (900 gastos em 31 dias projetavam 899,93).
    average_spend_raw = (outflow_to_today / Decimal(progress_day)) if progress_day > 0 else Decimal("0")
    current_average_spend = round_money(average_spend_raw)
    projected_closing = round_money(average_spend_raw * Decimal(total_days))
    allowed_remaining = round_money(available_budget - outflow_to_today)

    if available_budget <= 0 and projected_closing > 0:
        status_name = "red"
    elif available_budget <= 0 or projected_closing <= available_budget:
        status_name = "green"
    elif projected_closing <= available_budget * Decimal("1.10"):
        status_name = "yellow"
    else:
        status_name = "red"

    for day_number in range(1, total_days + 1):
        day_totals = day_map.get(day_number, {"income": Decimal("0"), "expense": Decimal("0")})
        income = round_money(day_totals["income"])
        spent = round_money(day_totals["expense"])
        net = round_money(income - spent)
        remaining = round_money(target_daily_goal - spent)
        progress = float(min(Decimal("100"), (spent / target_daily_goal) * Decimal("100"))) if target_daily_goal > 0 else 0.0
        day_status = "over" if spent > target_daily_goal else ("empty" if spent == 0 else "ok")
        days.append(
            {
                "day": day_number,
                "spent": spent,
                "income": income,
                "expense": spent,
                "net": net,
                "dailyGoalDelta": remaining,
                "remaining": remaining,
                "progress": progress,
                "status": day_status,
            }
        )

    days_above_goal = len([day for day in days if to_decimal(day["spent"]) > target_daily_goal])
    days_below_goal = len([day for day in days if Decimal("0") < to_decimal(day["spent"]) <= target_daily_goal])
    risk_alert = {
        "green": "Seu mês está dentro do orçamento planejado.",
        "yellow": "A projeção está até 10% acima do orçamento.",
        "red": "A projeção passa de 10% acima do orçamento.",
    }[status_name]

    return {
        "month": month,
        "dailyGoal": legacy_daily_goal,
        "reserveAmount": reserve_amount,
        "monthlyBudget": available_budget,
        "availableBudget": available_budget,
        "recommendedDailyGoal": recommended_daily_goal,
        "targetDailyGoal": target_daily_goal,
        "allowedRemaining": allowed_remaining,
        "daysAboveGoal": days_above_goal,
        "daysBelowGoal": days_below_goal,
        "currentAverageSpend": current_average_spend,
        "projectedClosing": projected_closing,
        "goalStatus": status_name,
        "riskAlert": risk_alert,
        "totalOutflow": outflow,
        "outflowToToday": outflow_to_today,
        "progressDay": progress_day,
        "totalDays": total_days,
        "days": days,
    }

