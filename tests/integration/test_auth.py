from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.conftest import TEST_DB_URL, register_user

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_register_creates_user(client):
    email = f"new-{uuid.uuid4().hex}@example.test"
    response = await client.post("/api/auth/register", json={"name": "Teste", "email": email, "password": "Senha123", "accept_terms": True})
    assert response.status_code == 201
    assert "access_token" in response.json()


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    email = f"dupe-{uuid.uuid4().hex}@example.test"
    await register_user(client, email)
    response = await client.post("/api/auth/register", json={"name": "Teste", "email": email, "password": "Senha123", "accept_terms": True})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_login_correct_credentials(client):
    user = await register_user(client)
    response = await client.post("/api/auth/login", data={"email": user["email"], "password": "Senha123"})
    assert response.status_code == 200
    assert "access_token" in response.json()


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    user = await register_user(client)
    response = await client.post("/api/auth/login", data={"email": user["email"], "password": "errada"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_rate_limit(client):
    user = await register_user(client)
    statuses = []
    for _ in range(6):
        response = await client.post("/api/auth/login", data={"email": user["email"], "password": "errada"})
        statuses.append(response.status_code)
    assert statuses[-1] == 429


@pytest.mark.asyncio
async def test_get_me_authenticated(client, auth_headers):
    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    assert "hashed_password" not in response.json()


@pytest.mark.asyncio
async def test_security_headers_are_applied(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_health_reports_signing_secret_source(client):
    """SEC-05: a origem do segredo de assinatura fica visível em
    /api/health, mesmo em serverless (onde validate_runtime_config não
    roda). No ambiente de teste, JWT_SECRET_KEY está definida."""
    response = await client.get("/api/health")
    assert response.status_code == 200, response.text
    assert response.json()["checks"]["signing"]["source"] == "env"


@pytest.mark.asyncio
async def test_register_omits_token_from_body_when_requested(client):
    """SEC-12: o SPA sinaliza que só precisa do cookie — o corpo da resposta
    não deveria carregar o token à toa."""
    email = f"omit-{uuid.uuid4().hex}@example.test"
    response = await client.post(
        "/api/auth/register",
        json={"name": "Teste", "email": email, "password": "Senha123", "accept_terms": True},
        headers={"X-Token-Response": "omit"},
    )
    assert response.status_code == 201, response.text
    assert "access_token" not in response.json()
    assert response.json()["token_type"] == "bearer"
    assert "trevo_access_token=" in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_register_still_returns_token_without_the_header(client):
    """Clientes de API que dependem de Bearer continuam recebendo o token."""
    email = f"keep-{uuid.uuid4().hex}@example.test"
    response = await client.post(
        "/api/auth/register",
        json={"name": "Teste", "email": email, "password": "Senha123", "accept_terms": True},
    )
    assert response.status_code == 201, response.text
    assert "access_token" in response.json()


@pytest.mark.asyncio
async def test_profile_photo_upload_validates_and_updates_user(client, auth_headers):
    png_content = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    response = await client.post(
        "/api/auth/me/avatar",
        headers=auth_headers,
        files={"file": ("perfil.png", png_content, "image/png")},
    )

    assert response.status_code == 200, response.text
    avatar_url = response.json()["avatar_url"]
    assert avatar_url.startswith("/media/profile-photos/")

    me_response = await client.get("/api/auth/me", headers=auth_headers)
    assert me_response.status_code == 200
    assert me_response.json()["avatar_url"] == avatar_url

    photo_response = await client.get(avatar_url)
    assert photo_response.status_code == 200
    assert photo_response.content == png_content

    clear_response = await client.put("/api/auth/me", headers=auth_headers, json={"avatar_url": None})
    assert clear_response.status_code == 200
    assert clear_response.json()["avatar_url"] is None


@pytest.mark.asyncio
async def test_profile_photo_rejects_invalid_file(client, auth_headers):
    response = await client.post(
        "/api/auth/me/avatar",
        headers=auth_headers,
        files={"file": ("perfil.txt", b"not an image", "text/plain")},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_get_me_unauthenticated(client):
    response = await client.get("/api/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_invalidates_token(client, auth_headers):
    response = await client.post("/api/auth/logout", headers=auth_headers)
    assert response.status_code == 200
    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_change_password_invalidates_existing_token(client):
    user = await register_user(client)
    headers = {"Authorization": f"Bearer {user['token']}"}
    await asyncio.sleep(1.1)

    response = await client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"current_password": "Senha123", "new_password": "NovaSenha123"},
    )
    assert response.status_code == 200, response.text

    old_token_response = await client.get("/api/auth/me", headers=headers)
    assert old_token_response.status_code == 401

    login_response = await client.post(
        "/api/auth/login",
        data={"email": user["email"], "password": "NovaSenha123"},
    )
    assert login_response.status_code == 200, login_response.text
