from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import PlainDictRoute, get_current_user
from app.core.database import db_cursor
from app.shared.money import round_money
from app.shared.serialization import normalize_row
from app.users.schemas import SettingsPayload
from app.users.service import get_settings

router = APIRouter(route_class=PlainDictRoute)


@router.post("/api/settings")
def save_settings(payload: SettingsPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    current = get_settings(user_id)
    monthly_income = round_money(payload.monthlyIncome if payload.monthlyIncome is not None else current["monthly_income"])
    daily_goal = round_money(payload.dailyGoal if payload.dailyGoal is not None else current["daily_goal"])
    reserve_amount = round_money(payload.reserveAmount if payload.reserveAmount is not None else current["reserve_amount"])
    reserve_goal_amount = round_money(
        payload.reserveGoalAmount if payload.reserveGoalAmount is not None else current.get("reserve_goal_amount", 0)
    )
    reserve_current_amount = round_money(
        payload.reserveCurrentAmount if payload.reserveCurrentAmount is not None else current.get("reserve_current_amount", 0)
    )

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE settings
            SET monthly_income = %s,
                daily_goal = %s,
                reserve_amount = %s,
                reserve_goal_amount = %s,
                reserve_current_amount = %s
            WHERE user_id = %s AND id = 1
            RETURNING *
            """,
            (monthly_income, daily_goal, reserve_amount, reserve_goal_amount, reserve_current_amount, user_id),
        )
        row = normalize_row(cursor.fetchone())
    return row or get_settings(user_id)

