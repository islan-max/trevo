from __future__ import annotations

import pytest

from tests.conftest import TEST_DB_URL

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_create_and_delete_transaction(client, auth_headers):
    payload = {
        "title": "Mercado",
        "amount": 120.5,
        "type": "expense",
        "categoryId": None,
        "paymentMethod": "pix",
        "transactionDate": "2024-05-10",
        "notes": "",
        "cardId": None,
        "billingMonth": None,
        "isRecurring": False,
    }
    response = await client.post("/api/transactions", headers=auth_headers, json=payload)
    assert response.status_code == 200, response.text
    transaction_id = response.json()["id"]
    response = await client.delete(f"/api/transactions/{transaction_id}", headers=auth_headers)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_delete_installment_requires_group_scope_confirmation(client, auth_headers):
    """FIN-10: apagar uma parcela apagava o grupo inteiro sem aviso prévio —
    agora exige ?scope=group, e sem ele responde 409 com a contagem."""
    card_response = await client.post(
        "/api/cards",
        headers=auth_headers,
        json={
            "name": "Cartão",
            "brand": "Visa",
            "lastFour": "1234",
            "creditLimit": 5000,
            "closingDay": 20,
            "dueDay": 28,
            "color": "#171717",
        },
    )
    assert card_response.status_code == 200, card_response.text
    card_id = card_response.json()["id"]

    installments_response = await client.post(
        f"/api/cards/{card_id}/installments",
        headers=auth_headers,
        json={
            "title": "Geladeira",
            "totalAmount": 900,
            "totalInstallments": 3,
            "purchaseDate": "2026-09-05",
        },
    )
    assert installments_response.status_code == 200, installments_response.text
    first_installment_id = installments_response.json()["rows"][0]["id"]

    without_scope = await client.delete(f"/api/transactions/{first_installment_id}", headers=auth_headers)
    assert without_scope.status_code == 409, without_scope.text
    assert "3" in without_scope.json()["detail"]

    with_scope = await client.delete(
        f"/api/transactions/{first_installment_id}",
        headers=auth_headers,
        params={"scope": "group"},
    )
    assert with_scope.status_code == 200, with_scope.text
    assert with_scope.json()["deletedGroup"] is True

    remaining = await client.get(
        "/api/transactions", headers=auth_headers, params={"month": "2026-09"}
    )
    assert all(row["installment_group"] is None for row in remaining.json())


@pytest.mark.asyncio
async def test_update_transaction_rejects_type_and_amount_change_on_installment(client, auth_headers):
    """FIN-09: editar type, amount ou billingMonth de UMA parcela isolada
    quebrava a integridade do grupo inteiro (soma deixava de bater com o
    total parcelado). Título, categoria e forma de pagamento continuam
    livres."""
    card_response = await client.post(
        "/api/cards",
        headers=auth_headers,
        json={
            "name": "Cartão",
            "brand": "Visa",
            "lastFour": "1234",
            "creditLimit": 5000,
            "closingDay": 20,
            "dueDay": 28,
            "color": "#171717",
        },
    )
    assert card_response.status_code == 200, card_response.text
    card_id = card_response.json()["id"]

    installments_response = await client.post(
        f"/api/cards/{card_id}/installments",
        headers=auth_headers,
        json={
            "title": "Notébook",
            "totalAmount": 900,
            "totalInstallments": 3,
            "purchaseDate": "2026-09-05",
        },
    )
    assert installments_response.status_code == 200, installments_response.text
    first_installment_id = installments_response.json()["rows"][0]["id"]

    reject_amount = await client.put(
        f"/api/transactions/{first_installment_id}", headers=auth_headers, json={"amount": 500}
    )
    assert reject_amount.status_code == 409, reject_amount.text

    reject_type = await client.put(
        f"/api/transactions/{first_installment_id}", headers=auth_headers, json={"type": "income"}
    )
    assert reject_type.status_code == 409, reject_type.text

    reject_billing_month = await client.put(
        f"/api/transactions/{first_installment_id}", headers=auth_headers, json={"billingMonth": "2026-12"}
    )
    assert reject_billing_month.status_code == 409, reject_billing_month.text

    allowed = await client.put(
        f"/api/transactions/{first_installment_id}",
        headers=auth_headers,
        json={"title": "Notébook (promoção)", "paymentMethod": "débito"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["title"] == "Notébook (promoção)"


@pytest.mark.asyncio
async def test_update_transaction_recalculates_duplicate_hash_for_imported_row(client, auth_headers):
    """FIN-09: editar um lançamento importado sem recalcular duplicate_hash
    deixa o hash obsoleto — uma reimportação do mesmo extrato reintroduziria
    a transação, já que o hash guardado não bate mais com o conteúdo atual."""
    upload = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={
            "file": (
                "extrato.csv",
                b"data;descricao;valor;tipo\n2024-05-01;Salario;3000;entrada\n",
                "text/csv",
            )
        },
    )
    assert upload.status_code == 200, upload.text
    mapping = {"date": "data", "description": "descricao", "value": "valor", "type": "tipo"}
    confirm = await client.post(
        "/api/imports/csv/confirm",
        headers=auth_headers,
        json={"importToken": upload.json()["importToken"], "mapping": mapping},
    )
    assert confirm.status_code == 200, confirm.text
    transaction = confirm.json()["transactions"][0]
    original_hash = transaction["duplicate_hash"]

    updated = await client.put(
        f"/api/transactions/{transaction['id']}",
        headers=auth_headers,
        json={"amount": 3500},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["duplicate_hash"] != original_hash
