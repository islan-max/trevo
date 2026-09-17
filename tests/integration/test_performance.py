"""PERF-02/PERF-03: /api/bootstrap fazia ~150 queries com 3 cartões e 20
grupos de parcelamento (get_cards_summary sozinho: 2 por cartão + 1 por
grupo, chamado duas vezes por request). Depois de BP-06 (batching por
card_id + cache de escopo de request), o total não escala mais com o número
de cartões/grupos."""

from __future__ import annotations

import pytest
from psycopg2.extras import RealDictCursor

from tests.conftest import TEST_DB_URL, register_user

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_bootstrap_query_count_does_not_scale_with_cards_and_installments(client, monkeypatch):
    user = await register_user(client)
    headers = {"Authorization": f"Bearer {user['token']}"}

    card_ids = []
    for i in range(3):
        card_response = await client.post(
            "/api/cards",
            headers=headers,
            json={
                "name": f"Cartão {i}",
                "brand": "Visa",
                "lastFour": "1234",
                "creditLimit": 5000,
                "closingDay": 20,
                "dueDay": 28,
                "color": "#171717",
            },
        )
        assert card_response.status_code == 200, card_response.text
        card_ids.append(card_response.json()["id"])

    for i in range(20):
        card_id = card_ids[i % len(card_ids)]
        response = await client.post(
            f"/api/cards/{card_id}/installments",
            headers=headers,
            json={
                "title": f"Compra {i}",
                "totalAmount": 300,
                "totalInstallments": 3,
                "purchaseDate": "2026-09-05",
            },
        )
        assert response.status_code == 200, response.text

    original_execute = RealDictCursor.execute
    call_count = {"value": 0}

    def counting_execute(self, *args, **kwargs):
        call_count["value"] += 1
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(RealDictCursor, "execute", counting_execute)

    response = await client.get("/api/bootstrap", headers=headers)
    assert response.status_code == 200, response.text

    assert call_count["value"] < 40, f"/api/bootstrap fez {call_count['value']} queries (esperado < 40)"
