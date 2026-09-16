from __future__ import annotations

import pytest

from tests.conftest import TEST_DB_URL

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_revoking_terms_privacy_deactivates_account(client, auth_headers):
    """SEC-09: revogar o consentimento que sustenta o próprio serviço não
    pode ser um no-op. Antes, a conta continuava plenamente ativa depois de
    'revogada' — agora é desativada (reversível, diferente de excluir) e a
    sessão atual é encerrada."""
    response = await client.post(
        "/api/privacy/consent",
        headers=auth_headers,
        json={"scope": "terms_privacy", "granted": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["accountDeactivated"] is True

    me_response = await client.get("/api/auth/me", headers=auth_headers)
    assert me_response.status_code == 401


@pytest.mark.asyncio
async def test_revoking_monthly_summary_does_not_deactivate_account(client, auth_headers):
    response = await client.post(
        "/api/privacy/consent",
        headers=auth_headers,
        json={"scope": "monthly_summary", "granted": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["accountDeactivated"] is False

    me_response = await client.get("/api/auth/me", headers=auth_headers)
    assert me_response.status_code == 200


@pytest.mark.asyncio
async def test_granting_terms_privacy_does_not_deactivate_account(client, auth_headers):
    response = await client.post(
        "/api/privacy/consent",
        headers=auth_headers,
        json={"scope": "terms_privacy", "granted": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["accountDeactivated"] is False
