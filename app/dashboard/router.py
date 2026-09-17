from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import PlainDictRoute, get_current_user
from app.auth.service import ensure_user_defaults
from app.budgets.service import get_budget_summary
from app.cards.service import get_cards_summary
from app.categories.service import list_categories
from app.dashboard.service import calculate_score, get_alerts_for_month, get_dashboard
from app.shared.dates import add_months, get_current_month
from app.shared.validation import validate_month_text
from app.transactions.service import get_recurring_suggestions, list_transactions
from app.users.service import get_settings

router = APIRouter(route_class=PlainDictRoute)


@router.get("/api/bootstrap")
def bootstrap(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    ensure_user_defaults(user_id)
    score = calculate_score(user_id, month_key)
    previous_score = calculate_score(user_id, add_months(month_key, -1))
    return {
        "settings": get_settings(user_id),
        "categories": list_categories(user_id),
        "cards": get_cards_summary(user_id, month_key),
        "transactions": list_transactions(user_id, month_key)[0],
        "dashboard": get_dashboard(user_id, month_key),
        "budget": get_budget_summary(user_id, month_key),
        "score": score,
        "previousScore": previous_score,
        "alerts": get_alerts_for_month(user_id, month_key),
        "recurringSuggestions": get_recurring_suggestions(user_id, month_key),
        "user": current_user,
    }



@router.get("/api/score")
def score(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return calculate_score(current_user["id"], month_key)



@router.get("/api/alerts")
def alerts(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    month_key = validate_month_text(month) or get_current_month()
    return get_alerts_for_month(current_user["id"], month_key)

