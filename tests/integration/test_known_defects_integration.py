"""Testes de integração que documentam defeitos conhecidos e ainda não
corrigidos, exercitando o fluxo real (HTTP + banco).

`xfail(strict=True)`: falha hoje pelo motivo descrito, e vira falha de suíte
se passar sem que o xfail seja removido — o lembrete para tirar a marca no
breakpoint que aplica a correção.

Ver docs/auditoria-2026-09.md para o achado completo de cada ID.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import TEST_DB_URL, register_user

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "csv"


async def _auth_headers(client) -> dict[str, str]:
    user = await register_user(client)
    return {"Authorization": f"Bearer {user['token']}"}


async def _create_card(client, headers: dict[str, str], *, closing_day: int = 20) -> int:
    payload = {
        "name": "Cartão de teste",
        "brand": "Visa",
        "lastFour": "1234",
        "creditLimit": 5000,
        "closingDay": closing_day,
        "dueDay": 28,
        "color": "#171717",
    }
    response = await client.post("/api/cards", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "DATA-01: o modo 'replace' da importação de CSV apaga TODOS os "
        "lançamentos do mês (WHERE ... = ANY(months), sem filtro por "
        "source), incluindo lançamentos manuais e parcelas de cartão que "
        "não vieram do arquivo."
    ),
)
async def test_csv_replace_mode_preserves_manual_transactions(client):
    headers = await _auth_headers(client)

    manual = await client.post(
        "/api/transactions",
        headers=headers,
        json={
            "title": "Aluguel",
            "amount": 1800,
            "type": "expense",
            "paymentMethod": "pix",
            "transactionDate": "2026-09-01",
        },
    )
    assert manual.status_code == 200, manual.text
    manual_id = manual.json()["id"]

    content = (FIXTURES_DIR / "pt_br.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=headers, files={"file": ("pt_br.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    token = upload.json()["importToken"]
    mapping = {"date": "Data", "description": "Descricao", "value": "Valor", "type": "Tipo"}

    confirm = await client.post(
        "/api/imports/csv/confirm",
        headers=headers,
        json={"importToken": token, "mapping": mapping, "mode": "replace"},
    )
    assert confirm.status_code == 200, confirm.text

    remaining = await client.get("/api/transactions", headers=headers, params={"month": "2026-09"})
    remaining_ids = [row["id"] for row in remaining.json()]
    assert manual_id in remaining_ids


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "DOM-01: installment_group é montado como "
        "f'{user_id}-{card_id}-{title}-{purchase_date}' — uma chave "
        "natural derivada de texto do usuário. Duas compras parceladas "
        "idênticas (mesmo título, cartão e data) colidem no mesmo grupo."
    ),
)
async def test_installment_group_does_not_collide_between_distinct_purchases(client):
    headers = await _auth_headers(client)
    card_id = await _create_card(client, headers)

    installment_payload = {
        "title": "Passagem aérea",
        "totalAmount": 1200,
        "totalInstallments": 3,
        "purchaseDate": "2026-09-05",
    }

    first = await client.post(f"/api/cards/{card_id}/installments", headers=headers, json=installment_payload)
    assert first.status_code == 200, first.text
    second = await client.post(f"/api/cards/{card_id}/installments", headers=headers, json=installment_payload)
    assert second.status_code == 200, second.text

    assert first.json()["group"] != second.json()["group"]
    assert second.json()["createdInstallments"] == 3


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "DOM-02: a taxa de juros é arredondada para centavos "
        "(round_money(Decimal(rate)/100)) antes de ser aplicada. Uma taxa "
        "de 0,4% a.m. é quantizada para 0,00% e os juros somem."
    ),
)
async def test_low_interest_rate_still_produces_interest(client):
    headers = await _auth_headers(client)

    payload = {
        "totalAmount": 1000,
        "totalInstallments": 3,
        "interestRate": 0.4,
        "purchaseDate": "2026-09-05",
        "months": 3,
    }
    response = await client.post("/api/installments/simulate", headers=headers, json=payload)
    assert response.status_code == 200, response.text

    assert sum(response.json()["installments"]) > 1000


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "DOM-03: create_transaction grava billing_month como veio no "
        "payload e nunca chama first_billing_month, apesar de aceitar "
        "cardId. Uma compra avulsa no crédito feita após o fechamento cai "
        "na fatura do mês da compra em vez da seguinte."
    ),
)
async def test_card_purchase_after_closing_day_bills_next_month(client):
    headers = await _auth_headers(client)
    card_id = await _create_card(client, headers, closing_day=20)

    response = await client.post(
        "/api/transactions",
        headers=headers,
        json={
            "title": "Compra após o fechamento",
            "amount": 150,
            "type": "expense",
            "paymentMethod": "credito",
            "transactionDate": "2026-09-25",  # depois do fechamento (dia 20)
            "cardId": card_id,
        },
    )
    assert response.status_code == 200, response.text

    assert response.json()["billing_month"] == "2026-10"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "SEC-03: create_category faz ON CONFLICT (user_id, name) DO "
        "UPDATE SET type = EXCLUDED.type — criar uma categoria com o "
        "nome de uma categoria ATIVA existente reescreve o type dela em "
        "vez de responder 409, reclassificando o histórico ligado a ela."
    ),
)
async def test_create_category_does_not_overwrite_active_category_type(client):
    headers = await _auth_headers(client)

    first = await client.post(
        "/api/categories",
        headers=headers,
        json={"name": "Categoria Teste XPTO", "type": "expense", "color": "#2E9D5B", "icon": "🛒"},
    )
    assert first.status_code == 200, first.text
    category_id = first.json()["id"]

    conflicting = await client.post(
        "/api/categories",
        headers=headers,
        json={"name": "Categoria Teste XPTO", "type": "income", "color": "#2E9D5B", "icon": "💼"},
    )
    assert conflicting.status_code == 409, conflicting.text

    bootstrap = await client.get("/api/bootstrap", headers=headers)
    category = next(c for c in bootstrap.json()["categories"] if c["id"] == category_id)
    assert category["type"] == "expense"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "CSV-04: parse_csv_rows sempre lê a linha 1 como cabeçalho "
        "(csv.DictReader puro). Extratos bancários reais trazem linhas de "
        "preâmbulo (nome do banco, período, agência) antes da linha de "
        "colunas, e a importação falha ou produz colunas absurdas."
    ),
)
async def test_csv_upload_skips_bank_preamble_before_header(client):
    headers = await _auth_headers(client)

    content = (FIXTURES_DIR / "preambulo.csv").read_bytes()
    response = await client.post(
        "/api/imports/csv/upload", headers=headers, files={"file": ("preambulo.csv", content, "text/csv")}
    )
    assert response.status_code == 200, response.text

    assert set(response.json()["columns"]) == {"Data", "Historico", "Valor"}
    assert response.json()["totalRows"] == 2


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "FIN-01: build_duplicate_hash não inclui hora nem conta. Duas "
        "transações legítimas e distintas no mesmo dia (mesma descrição e "
        "valor, hora diferente) produzem o mesmo hash e uma é descartada "
        "como duplicata."
    ),
)
async def test_csv_import_keeps_distinct_transactions_with_different_times(client):
    headers = await _auth_headers(client)

    content = (FIXTURES_DIR / "duplicatas.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=headers, files={"file": ("duplicatas.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    token = upload.json()["importToken"]
    mapping = {"date": "Data", "description": "Descricao", "value": "Valor", "time": "Hora"}

    confirm = await client.post(
        "/api/imports/csv/confirm",
        headers=headers,
        json={"importToken": token, "mapping": mapping, "mode": "merge"},
    )
    assert confirm.status_code == 200, confirm.text

    assert confirm.json()["imported"] == 2
