"""Testes de integração que documentam defeitos conhecidos e ainda não
corrigidos, exercitando o fluxo real (HTTP + banco).

`xfail(strict=True)`: falha hoje pelo motivo descrito, e vira falha de suíte
se passar sem que o xfail seja removido — o lembrete para tirar a marca no
breakpoint que aplica a correção.

Ver docs/auditoria-2026-09.md para o achado completo de cada ID.

DATA-01, DOM-01 e SEC-03 foram corrigidos no BP-02 e seus testes promovidos
para test_csv_import.py, test_cards.py e test_categories.py, respectivamente.
DOM-02 e DOM-03 foram corrigidos no BP-03 e seus testes promovidos para
test_cards.py.
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
