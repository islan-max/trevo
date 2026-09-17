"""FIN-03: calculate_score identificava a categoria de reserva/investimentos
pelo NOME (`lower(c.name) IN ('reserva', 'investimentos')`) — renomear a
categoria zerava silenciosamente esse eixo do Ritmo Score. Agora usa
categories.role (BP-08), atribuído uma vez às categorias padrão e que não
muda se o usuário renomear a categoria depois.
"""

from __future__ import annotations

import pytest

from app.auth.service import get_user_by_email
from app.core.database import db_cursor
from app.dashboard.service import calculate_score
from app.shared.dates import get_current_month
from tests.conftest import TEST_DB_URL, register_user

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_score_reserve_axis_survives_category_rename(client):
    user = await register_user(client)
    headers = {"Authorization": f"Bearer {user['token']}"}
    month = get_current_month()

    settings_response = await client.post("/api/settings", headers=headers, json={"monthlyIncome": 3000})
    assert settings_response.status_code == 200, settings_response.text

    bootstrap = await client.get("/api/bootstrap", headers=headers)
    assert bootstrap.status_code == 200, bootstrap.text
    reserve_category = next(c for c in bootstrap.json()["categories"] if c["name"] == "Reserva")

    tx_response = await client.post(
        "/api/transactions",
        headers=headers,
        json={
            "title": "Guardar dinheiro",
            "amount": 500,
            "type": "expense",
            "paymentMethod": "pix",
            "transactionDate": f"{month}-10",
            "categoryId": reserve_category["id"],
        },
    )
    assert tx_response.status_code == 200, tx_response.text

    user_id = get_user_by_email(user["email"])["id"]

    score_before_rename = calculate_score(user_id, month)
    assert score_before_rename["breakdown"]["reservas"] > 0

    with db_cursor(commit=True) as cursor:
        cursor.execute("UPDATE categories SET name = %s WHERE id = %s", ("Guardado no banco", reserve_category["id"]))

    score_after_rename = calculate_score(user_id, month)
    assert score_after_rename["breakdown"]["reservas"] == score_before_rename["breakdown"]["reservas"]
