"""SEC-04: vinculação de conta social não pode ser automática.

`resolve_oauth_user` (usado no login/cadastro social) e
`link_oauth_identity_to_user` (usado por /api/auth/oauth/{provider}/authorize
?link=true, autenticado) são funções puras o bastante para testar sem
precisar simular a troca de código com um provedor real — o perfil já
resolvido (e-mail, subject, provider) é o único contrato entre as duas
metades do fluxo.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.main import get_user_by_email, link_oauth_identity_to_user, resolve_oauth_user
from tests.conftest import TEST_DB_URL, register_user

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_resolve_oauth_user_does_not_silently_link_password_account(client):
    """Achado original: o e-mail de uma conta com senha própria nunca
    vinculada a nenhum provedor social vinculava automaticamente no login
    OAuth — hoje responde 409 e não altera a conta."""
    user = await register_user(client)

    with pytest.raises(HTTPException) as exc_info:
        resolve_oauth_user(
            {"provider": "google", "subject": "google-subject-123", "email": user["email"], "name": "Teste"}
        )
    assert exc_info.value.status_code == 409

    unchanged = get_user_by_email(user["email"])
    assert unchanged["auth_provider"] is None
    assert unchanged["oauth_subject"] is None


@pytest.mark.asyncio
async def test_resolve_oauth_user_still_logs_in_returning_oauth_user(client):
    """Não regride o caso legítimo: um e-mail JÁ vinculado ao mesmo provedor
    continua autenticando normalmente (sem tentar vincular de novo)."""
    email = f"oauth-{uuid.uuid4().hex}@example.test"
    profile = {"provider": "google", "subject": f"subj-{uuid.uuid4().hex}", "email": email, "name": "Teste OAuth"}

    first = resolve_oauth_user(profile)
    second = resolve_oauth_user(profile)

    assert first["id"] == second["id"]
    assert second["auth_provider"] == "google"


@pytest.mark.asyncio
async def test_link_oauth_identity_to_user_links_authenticated_account(client, auth_headers):
    me = await client.get("/api/auth/me", headers=auth_headers)
    assert me.status_code == 200, me.text
    user_id = me.json()["id"]

    link_oauth_identity_to_user(user_id, {"provider": "github", "subject": f"gh-{uuid.uuid4().hex}"})

    updated = await client.get("/api/auth/me", headers=auth_headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["auth_provider"] == "github"


@pytest.mark.asyncio
async def test_link_oauth_identity_rejects_identity_already_linked_to_another_user(client):
    user_a = await register_user(client)
    user_b = await register_user(client)
    user_a_id = get_user_by_email(user_a["email"])["id"]
    user_b_id = get_user_by_email(user_b["email"])["id"]
    subject = f"shared-{uuid.uuid4().hex}"

    link_oauth_identity_to_user(user_a_id, {"provider": "facebook", "subject": subject})

    with pytest.raises(HTTPException) as exc_info:
        link_oauth_identity_to_user(user_b_id, {"provider": "facebook", "subject": subject})
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_oauth_link_authorize_requires_authentication(client, monkeypatch):
    monkeypatch.setenv("OAUTH_REDIRECT_BASE_URL", "http://test")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")

    response = await client.get("/api/auth/oauth/google/authorize?link=true", follow_redirects=False)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oauth_link_authorize_succeeds_when_authenticated(client, auth_headers, monkeypatch):
    monkeypatch.setenv("OAUTH_REDIRECT_BASE_URL", "http://test")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")

    response = await client.get(
        "/api/auth/oauth/google/authorize?link=true", headers=auth_headers, follow_redirects=False
    )
    assert response.status_code == 302
    assert "https://accounts.google.com" in response.headers["location"]
