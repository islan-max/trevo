"""Exercita o caminho de autenticação que a produção realmente usa.

Todos os outros testes de integração autenticam com `Authorization: Bearer`,
que é explicitamente isento do middleware CSRF (ver `has_bearer` em
`app/main.py::csrf_protect`). O SPA nunca manda Bearer — ele usa o cookie
HttpOnly mais o header `X-CSRF-Token` (double-submit). Sem estes testes, o
caminho de produção nunca era exercitado. Ver TEST-01 na auditoria técnica.
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DB_URL, csrf_headers, register_user

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")

_TRANSACTION_PAYLOAD = {
    "title": "Compra via sessão de cookie",
    "amount": 42.5,
    "type": "expense",
    "paymentMethod": "pix",
    "transactionDate": "2026-09-10",
}


@pytest.mark.asyncio
async def test_cookie_session_authenticates_without_bearer(cookie_client):
    await register_user(cookie_client)

    response = await cookie_client.get("/api/auth/me")

    assert response.status_code == 200, response.text
    assert "hashed_password" not in response.json()


@pytest.mark.asyncio
async def test_cookie_mutation_requires_csrf_header(cookie_client):
    await register_user(cookie_client)

    without_csrf = await cookie_client.post("/api/transactions", json=_TRANSACTION_PAYLOAD)
    assert without_csrf.status_code == 403, without_csrf.text
    assert "csrf" in without_csrf.json()["detail"].lower()

    with_csrf = await cookie_client.post(
        "/api/transactions", json=_TRANSACTION_PAYLOAD, headers=csrf_headers(cookie_client)
    )
    assert with_csrf.status_code == 200, with_csrf.text


@pytest.mark.asyncio
async def test_cookie_wrong_csrf_token_is_rejected(cookie_client):
    await register_user(cookie_client)

    response = await cookie_client.post(
        "/api/transactions",
        json=_TRANSACTION_PAYLOAD,
        headers={"X-CSRF-Token": "token-forjado-nao-bate-com-o-cookie"},
    )

    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_cookie_logout_clears_session(cookie_client):
    await register_user(cookie_client)

    logout_response = await cookie_client.post("/api/auth/logout", headers=csrf_headers(cookie_client))
    assert logout_response.status_code == 200, logout_response.text

    me_response = await cookie_client.get("/api/auth/me")
    assert me_response.status_code == 401


@pytest.mark.asyncio
async def test_cookie_csrf_endpoint_issues_token_for_authenticated_session(cookie_client):
    await register_user(cookie_client)

    response = await cookie_client.get("/api/auth/csrf")

    assert response.status_code == 200, response.text
    assert response.json()["csrf_token"]
