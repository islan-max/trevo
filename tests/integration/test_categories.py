from __future__ import annotations

import pytest

from tests.conftest import TEST_DB_URL

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_create_category_conflicts_with_active_category_of_same_name(client, auth_headers):
    """SEC-03: o antigo ON CONFLICT (user_id, name) DO UPDATE SET type =
    EXCLUDED.type reescrevia o type de uma categoria ATIVA existente,
    reclassificando o histórico ligado a ela. Agora responde 409 e não
    altera nada."""
    first = await client.post(
        "/api/categories",
        headers=auth_headers,
        json={"name": "Categoria Teste XPTO", "type": "expense", "color": "#2E9D5B", "icon": "\U0001f6d2"},
    )
    assert first.status_code == 200, first.text
    category_id = first.json()["id"]

    conflicting = await client.post(
        "/api/categories",
        headers=auth_headers,
        json={"name": "Categoria Teste XPTO", "type": "income", "color": "#2E9D5B", "icon": "\U0001f4bc"},
    )
    assert conflicting.status_code == 409, conflicting.text

    bootstrap = await client.get("/api/bootstrap", headers=auth_headers)
    category = next(c for c in bootstrap.json()["categories"] if c["id"] == category_id)
    assert category["type"] == "expense"


@pytest.mark.asyncio
async def test_create_category_reactivates_archived_category_without_changing_type(client, auth_headers):
    created = await client.post(
        "/api/categories",
        headers=auth_headers,
        json={"name": "Categoria Arquivável", "type": "income", "color": "#2E9D5B", "icon": "\U0001f4bc"},
    )
    assert created.status_code == 200, created.text
    category_id = created.json()["id"]

    # delete_category só arquiva (em vez de apagar de vez) quando há
    # lançamento vinculado — precisamos de um para exercitar a reativação.
    linked_transaction = await client.post(
        "/api/transactions",
        headers=auth_headers,
        json={
            "title": "Recebimento",
            "amount": 500,
            "type": "income",
            "categoryId": category_id,
            "paymentMethod": "pix",
            "transactionDate": "2026-09-10",
        },
    )
    assert linked_transaction.status_code == 200, linked_transaction.text

    deleted = await client.delete(f"/api/categories/{category_id}", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["archived"] is True

    recreated = await client.post(
        "/api/categories",
        headers=auth_headers,
        # Tenta trocar o type na "recriação" — deve ser ignorado.
        json={"name": "Categoria Arquivável", "type": "expense", "color": "#D1495B", "icon": "\U0001f3e0"},
    )
    assert recreated.status_code == 200, recreated.text
    assert recreated.json()["id"] == category_id
    assert recreated.json()["type"] == "income"
    assert recreated.json()["color"] == "#D1495B"
    assert recreated.json()["is_active"] is True
