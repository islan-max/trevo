from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import PlainDictRoute, get_current_user
from app.goals.service import get_goals
from app.shared.dates import get_current_month
from app.shared.validation import validate_month_text

router = APIRouter(route_class=PlainDictRoute)


@router.get("/api/goals")
def goals(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return get_goals(current_user["id"], month_key)

