from __future__ import annotations

from pathlib import Path

import pytest

from app.core.database import db_cursor
from app.integrations.normalizer import build_duplicate_hash
from tests.conftest import TEST_DB_URL

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "csv"


CSV_CONTENT = "data;descricao;valor;tipo\n2024-05-01;Salario;3000;entrada\n02/05/2024;Mercado;-125,50;saida\n"
CSV_MIXED_NO_TYPE = (
    "data;descricao;valor\n"
    "2026-06-01;Salario;R$ 3.000,00\n"
    "01/07/2026;Mercado;R$ -100,00\n"
    "01-08-2026;Freela;1.250,90\n"
    "02/08/2026;Aluguel;-1.250,90\n"
)


async def upload_preview_confirm(client, auth_headers, expected_duplicates=0):
    upload_response = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={"file": ("extrato.csv", CSV_CONTENT.encode("utf-8"), "text/csv")},
    )
    assert upload_response.status_code == 200, upload_response.text
    upload = upload_response.json()
    assert upload["columns"] == ["data", "descricao", "valor", "tipo"]

    payload = {
        "importToken": upload["importToken"],
        "mapping": {"date": "data", "description": "descricao", "value": "valor", "type": "tipo"},
    }
    preview_response = await client.post("/api/imports/csv/preview", headers=auth_headers, json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["validRows"] == 2
    assert preview["invalidRows"] == 0
    assert preview["duplicateRows"] == expected_duplicates
    assert preview["preview"][1]["amount"] == 125.5
    assert preview["preview"][1]["type"] == "expense"
    assert preview["preview"][1]["detectedMonth"] == "2024-05"

    confirm_response = await client.post("/api/imports/csv/confirm", headers=auth_headers, json=payload)
    assert confirm_response.status_code == 200, confirm_response.text
    return confirm_response.json()


@pytest.mark.asyncio
async def test_csv_import_creates_transactions_and_skips_duplicates(client, auth_headers):
    first_result = await upload_preview_confirm(client, auth_headers)
    assert first_result["imported"] == 2
    assert first_result["duplicates"] == 0
    assert {row["source"] for row in first_result["transactions"]} == {"csv_import"}
    assert all(row["duplicate_hash"] for row in first_result["transactions"])

    # Na segunda passada as duas linhas já existem, então a prévia precisa acusá-las.
    second_result = await upload_preview_confirm(client, auth_headers, expected_duplicates=2)
    assert second_result["imported"] == 0
    assert second_result["duplicates"] == 2


@pytest.mark.asyncio
async def test_csv_preview_reports_existing_duplicates_before_confirm(client, auth_headers):
    await upload_preview_confirm(client, auth_headers)

    upload_response = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={"file": ("extrato.csv", CSV_CONTENT.encode("utf-8"), "text/csv")},
    )
    assert upload_response.status_code == 200, upload_response.text
    upload = upload_response.json()

    payload = {
        "importToken": upload["importToken"],
        "mapping": {"date": "data", "description": "descricao", "value": "valor", "type": "tipo"},
    }
    preview_response = await client.post("/api/imports/csv/preview", headers=auth_headers, json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()

    assert preview["validRows"] == 2
    assert preview["duplicateRows"] == 2
    assert len(preview["duplicates"]) == 2


@pytest.mark.asyncio
async def test_csv_import_detects_legacy_duplicate_hashes(client, auth_headers):
    me_response = await client.get("/api/auth/me", headers=auth_headers)
    assert me_response.status_code == 200, me_response.text
    user_id = me_response.json()["id"]
    legacy_hash = build_duplicate_hash(user_id, "2024-05-01", "Salario", 3000)

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            INSERT INTO transactions
              (user_id, title, amount, type, payment_method, transaction_date, source, external_id,
               imported_at, raw_description, duplicate_hash)
            VALUES (%s, 'Salario', 3000, 'income', 'csv_import', '2024-05-01', 'csv_import', %s,
                    NOW(), 'Salario', %s)
            """,
            (user_id, legacy_hash, legacy_hash),
        )

    upload_response = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={"file": ("extrato.csv", CSV_CONTENT.encode("utf-8"), "text/csv")},
    )
    assert upload_response.status_code == 200, upload_response.text
    payload = {
        "importToken": upload_response.json()["importToken"],
        "mapping": {"date": "data", "description": "descricao", "value": "valor", "type": "tipo"},
    }

    preview_response = await client.post("/api/imports/csv/preview", headers=auth_headers, json=payload)
    assert preview_response.status_code == 200, preview_response.text
    assert preview_response.json()["duplicateRows"] == 1

    confirm_response = await client.post("/api/imports/csv/confirm", headers=auth_headers, json=payload)
    assert confirm_response.status_code == 200, confirm_response.text
    result = confirm_response.json()
    assert result["duplicates"] == 1
    assert result["imported"] == 1


@pytest.mark.asyncio
async def test_csv_replace_mode_preserves_manual_and_installment_transactions(client, auth_headers):
    """DATA-01: 'substituir' apaga só o que veio de CSV, nunca lançamentos
    manuais nem parcelas de cartão que caiam no mesmo mês do arquivo."""
    manual = await client.post(
        "/api/transactions",
        headers=auth_headers,
        json={
            "title": "Aluguel",
            "amount": 1800,
            "type": "expense",
            "paymentMethod": "pix",
            "transactionDate": "2024-05-15",
        },
    )
    assert manual.status_code == 200, manual.text
    manual_id = manual.json()["id"]

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
            "totalAmount": 1200,
            "totalInstallments": 3,
            "purchaseDate": "2024-05-05",
        },
    )
    assert installments_response.status_code == 200, installments_response.text
    installment_id = installments_response.json()["rows"][0]["id"]

    upload_response = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={"file": ("extrato.csv", CSV_CONTENT.encode("utf-8"), "text/csv")},
    )
    assert upload_response.status_code == 200, upload_response.text
    payload = {
        "importToken": upload_response.json()["importToken"],
        "mapping": {"date": "data", "description": "descricao", "value": "valor", "type": "tipo"},
        "mode": "replace",
    }
    confirm_response = await client.post("/api/imports/csv/confirm", headers=auth_headers, json=payload)
    assert confirm_response.status_code == 200, confirm_response.text
    assert confirm_response.json()["imported"] == 2
    assert confirm_response.json()["replaced"] == 0

    remaining = await client.get("/api/transactions", headers=auth_headers, params={"month": "2024-05"})
    remaining_ids = {row["id"] for row in remaining.json()["items"]}
    assert manual_id in remaining_ids
    assert installment_id in remaining_ids


@pytest.mark.asyncio
async def test_csv_replace_mode_still_removes_previous_csv_import_same_month(client, auth_headers):
    """O modo 'substituir' continua substituindo importações anteriores —
    a correção de DATA-01 só protege o que NÃO veio de CSV."""
    first = await upload_preview_confirm(client, auth_headers)
    assert first["imported"] == 2

    upload_response = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={"file": ("extrato.csv", CSV_CONTENT.encode("utf-8"), "text/csv")},
    )
    assert upload_response.status_code == 200, upload_response.text
    payload = {
        "importToken": upload_response.json()["importToken"],
        "mapping": {"date": "data", "description": "descricao", "value": "valor", "type": "tipo"},
        "mode": "replace",
    }
    confirm_response = await client.post("/api/imports/csv/confirm", headers=auth_headers, json=payload)
    assert confirm_response.status_code == 200, confirm_response.text
    assert confirm_response.json()["replaced"] == 2
    assert confirm_response.json()["imported"] == 2


@pytest.mark.asyncio
async def test_csv_import_rejects_wrong_extension(client, auth_headers):
    response = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={"file": ("extrato.txt", CSV_CONTENT.encode("utf-8"), "text/csv")},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_csv_import_infers_type_and_preserves_original_dates(client, auth_headers):
    upload_response = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={"file": ("extrato.csv", CSV_MIXED_NO_TYPE.encode("utf-8"), "text/csv")},
    )
    assert upload_response.status_code == 200, upload_response.text
    upload = upload_response.json()
    payload = {
        "importToken": upload["importToken"],
        "mapping": {"date": "data", "description": "descricao", "value": "valor", "type": None},
    }

    preview_response = await client.post("/api/imports/csv/preview", headers=auth_headers, json=payload)
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert [(row["transactionDate"], row["detectedMonth"], row["type"], row["amount"]) for row in preview["preview"]] == [
        ("2026-06-01", "2026-06", "income", 3000.0),
        ("2026-07-01", "2026-07", "expense", 100.0),
        ("2026-08-01", "2026-08", "income", 1250.9),
        ("2026-08-02", "2026-08", "expense", 1250.9),
    ]

    confirm_response = await client.post("/api/imports/csv/confirm", headers=auth_headers, json=payload)
    assert confirm_response.status_code == 200, confirm_response.text
    transactions = confirm_response.json()["transactions"]
    assert [(row["transaction_date"], row["type"], row["amount"]) for row in transactions] == [
        ("2026-06-01", "income", 3000.0),
        ("2026-07-01", "expense", 100.0),
        ("2026-08-01", "income", 1250.9),
        ("2026-08-02", "expense", 1250.9),
    ]


@pytest.mark.asyncio
async def test_csv_upload_skips_bank_preamble_before_header(client, auth_headers):
    """CSV-04: parse_csv_rows sempre lia a linha 1 como cabeçalho. Extratos
    bancários reais trazem linhas de preâmbulo (nome do banco, período,
    agência) antes da linha de colunas — find_csv_header_line_index localiza
    a linha real."""
    content = (FIXTURES_DIR / "preambulo.csv").read_bytes()
    response = await client.post(
        "/api/imports/csv/upload", headers=auth_headers, files={"file": ("preambulo.csv", content, "text/csv")}
    )
    assert response.status_code == 200, response.text

    assert set(response.json()["columns"]) == {"Data", "Historico", "Valor"}
    assert response.json()["totalRows"] == 2


@pytest.mark.asyncio
async def test_csv_import_keeps_distinct_transactions_with_different_times(client, auth_headers):
    """FIN-01: build_duplicate_hash não incluía hora nem conta. Duas
    transações legítimas e distintas no mesmo dia (mesma descrição e valor,
    hora diferente) produziam o mesmo hash e uma era descartada como
    duplicata."""
    content = (FIXTURES_DIR / "duplicatas.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=auth_headers, files={"file": ("duplicatas.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    token = upload.json()["importToken"]
    mapping = {"date": "Data", "description": "Descricao", "value": "Valor", "time": "Hora"}

    confirm = await client.post(
        "/api/imports/csv/confirm",
        headers=auth_headers,
        json={"importToken": token, "mapping": mapping, "mode": "merge"},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["imported"] == 2


@pytest.mark.asyncio
async def test_csv_import_handles_en_us_thousands_separator(client, auth_headers):
    """CSV-01: formato en-US ("1,234.56") não vira mais 1,23456."""
    content = (FIXTURES_DIR / "en_us.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=auth_headers, files={"file": ("en_us.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    mapping = {"date": "Date", "description": "Description", "value": "Amount"}
    preview = await client.post(
        "/api/imports/csv/preview",
        headers=auth_headers,
        json={"importToken": upload.json()["importToken"], "mapping": mapping},
    )
    assert preview.status_code == 200, preview.text
    amounts = sorted(row["amount"] for row in preview.json()["preview"])
    assert amounts == [45.9, 1234.56]


@pytest.mark.asyncio
async def test_csv_import_handles_parentheses_as_negative(client, auth_headers):
    """CSV-02: valor negativo entre parênteses (comum em exports de cartão)
    é reconhecido como despesa mesmo sem coluna de tipo."""
    content = (FIXTURES_DIR / "parenteses.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=auth_headers, files={"file": ("parenteses.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    mapping = {"date": "Data", "description": "Descricao", "value": "Valor"}
    preview = await client.post(
        "/api/imports/csv/preview",
        headers=auth_headers,
        json={"importToken": upload.json()["importToken"], "mapping": mapping},
    )
    assert preview.status_code == 200, preview.text
    rows = {row["title"]: row for row in preview.json()["preview"]}
    assert rows["Estorno de compra"]["type"] == "expense"
    assert rows["Estorno de compra"]["amount"] == 123.45


@pytest.mark.asyncio
async def test_csv_import_handles_tab_delimiter(client, auth_headers):
    """CSV-05: delimitador tabulação, além de ';' e ','."""
    content = (FIXTURES_DIR / "tab.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=auth_headers, files={"file": ("tab.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["columns"] == ["Data", "Descricao", "Valor"]
    assert upload.json()["totalRows"] == 2


@pytest.mark.asyncio
async def test_csv_import_handles_utf8_bom(client, auth_headers):
    content = (FIXTURES_DIR / "utf8_bom.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=auth_headers, files={"file": ("utf8_bom.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    descriptions = {row["Descricao"] for row in upload.json()["preview"]}
    assert "Alimentação" in descriptions


@pytest.mark.asyncio
async def test_csv_import_handles_cp1252_encoding(client, auth_headers):
    """CSV-06: cp1252 antes do fallback final em latin-1, que nunca levanta
    mas decodifica aspas curvas e travessão como caracteres errados."""
    content = (FIXTURES_DIR / "cp1252.csv").read_bytes()
    upload = await client.post(
        "/api/imports/csv/upload", headers=auth_headers, files={"file": ("cp1252.csv", content, "text/csv")}
    )
    assert upload.status_code == 200, upload.text
    descriptions = {row["Descricao"] for row in upload.json()["preview"]}
    assert "Compra “promocional”" in descriptions


@pytest.mark.asyncio
async def test_csv_import_stores_account_separately_from_payment_method(client, auth_headers):
    """CSV-08/09: a conta do extrato vai para a coluna account, não mais
    payment_method — e external_id não recebe mais o duplicate_hash."""
    upload = await client.post(
        "/api/imports/csv/upload",
        headers=auth_headers,
        files={
            "file": (
                "extrato.csv",
                b"data;descricao;valor;conta\n2024-05-01;Salario;3000;Conta Corrente\n",
                "text/csv",
            )
        },
    )
    assert upload.status_code == 200, upload.text
    mapping = {"date": "data", "description": "descricao", "value": "valor", "account": "conta"}
    confirm = await client.post(
        "/api/imports/csv/confirm",
        headers=auth_headers,
        json={"importToken": upload.json()["importToken"], "mapping": mapping},
    )
    assert confirm.status_code == 200, confirm.text
    transaction = confirm.json()["transactions"][0]
    assert transaction["account"] == "Conta Corrente"
    assert transaction["payment_method"] == "csv_import"
    assert transaction["external_id"] is None
