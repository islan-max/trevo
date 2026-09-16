"""Testes de integração que documentam defeitos conhecidos e ainda não
corrigidos, exercitando o fluxo real (HTTP + banco).

`xfail(strict=True)`: falha hoje pelo motivo descrito, e vira falha de suíte
se passar sem que o xfail seja removido — o lembrete para tirar a marca no
breakpoint que aplica a correção.

Ver docs/auditoria-2026-09.md para o achado completo de cada ID.

DATA-01, DOM-01 e SEC-03 foram corrigidos no BP-02 e seus testes promovidos
para test_csv_import.py, test_cards.py e test_categories.py, respectivamente.
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
