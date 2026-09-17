from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-local-tests-32chars")
os.environ.setdefault("ENVIRONMENT", "testing")

from app.api.deps import limiter  # noqa: E402
from app.core.database import close_db_pool, connection, db_cursor, init_db_pool  # noqa: E402
from app.main import app  # noqa: E402
from migrate import run_migrations  # noqa: E402

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


def pytest_report_header(config):
    """Deixa explicito, no topo da saida, que a integracao nao vai rodar.

    Sem isto a suite fica verde sem ter executado os testes de integracao, o que
    passa uma sensacao de cobertura que nao existe.
    """
    if TEST_DB_URL:
        return "banco de teste: configurado — suíte completa"
    return (
        "banco de teste: AUSENTE — os testes de integração serão pulados. "
        "Defina TEST_DATABASE_URL para executá-los."
    )


def reset_rate_limits() -> None:
    storage = getattr(getattr(limiter, "_limiter", None), "storage", None)
    if storage and hasattr(storage, "reset"):
        storage.reset()


@pytest.fixture(scope="session", autouse=True)
def ensure_schema():
    """Garante o schema antes de qualquer teste.

    O `clean_db` abaixo é autouse e apaga tabelas mesmo em testes unitários; sem
    isto, rodar só `tests/unit` com o banco configurado quebrava com
    `relation "transactions" does not exist`, porque quem criava o schema era o
    startup — e ele só roda para quem pede o fixture `test_app`.
    """
    if not TEST_DB_URL:
        return
    os.environ["DATABASE_URL"] = TEST_DB_URL
    init_db_pool()
    with connection() as conn:
        run_migrations(conn)


@pytest_asyncio.fixture(scope="session")
async def test_app():
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL is not configured")
    os.environ["DATABASE_URL"] = TEST_DB_URL
    # `router.startup()`/`router.shutdown()` foram removidos do Starlette; o
    # lifespan_context é a forma atual de rodar o ciclo de vida (e é o que cria
    # o schema, via run_migrations).
    async with app.router.lifespan_context(app):
        yield app
    close_db_pool()


@pytest_asyncio.fixture(scope="session")
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest_asyncio.fixture
async def cookie_client(test_app):
    """Cliente autenticado por cookie HttpOnly + CSRF double-submit.

    É o caminho que a produção realmente usa (o SPA nunca manda Bearer). Ao
    contrário de `client` (escopo de sessão, para não recriar o app a cada
    teste), este é por-teste: httpx guarda cookies automaticamente no cliente,
    e um jar compartilhado entre testes vazaria sessão de um teste para o
    outro — um teste que espera 401 por estar deslogado passaria por engano se
    herdasse o cookie de autenticação de um teste anterior.
    """
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def csrf_headers(client: AsyncClient) -> dict[str, str]:
    """Extrai o token do cookie `trevo_csrf` já presente no client (double-submit).

    Chame depois de um login/registro bem-sucedido em `cookie_client` — a
    resposta de `set_auth_cookie` sempre emite esse cookie junto.
    """
    token = client.cookies.get("trevo_csrf")
    assert token, "Cookie trevo_csrf ausente — o cliente autenticou pelo caminho certo?"
    return {"X-CSRF-Token": token}


@pytest_asyncio.fixture(autouse=True)
async def clean_db(ensure_schema):
    if not TEST_DB_URL:
        yield
        return
    os.environ["DATABASE_URL"] = TEST_DB_URL
    init_db_pool()
    reset_rate_limits()
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            DELETE FROM transactions WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM card_pins WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM cards WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM categories WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM settings WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM login_failures_state;
            DELETE FROM rate_limit_state;
            DELETE FROM users WHERE email LIKE %s;
            """,
            ("%@example.test", "%@example.test", "%@example.test", "%@example.test", "%@example.test", "%@example.test"),
        )
    yield
    reset_rate_limits()
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            DELETE FROM transactions WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM card_pins WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM cards WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM categories WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM settings WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s);
            DELETE FROM login_failures_state;
            DELETE FROM rate_limit_state;
            DELETE FROM users WHERE email LIKE %s;
            """,
            ("%@example.test", "%@example.test", "%@example.test", "%@example.test", "%@example.test", "%@example.test"),
        )


@pytest_asyncio.fixture
async def auth_headers(client):
    email = f"user-{uuid.uuid4().hex}@example.test"
    payload = {"name": "Teste", "email": email, "password": "Senha123", "accept_terms": True}
    response = await client.post("/api/auth/register", json=payload)
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def register_user(client, email: str | None = None) -> dict:
    email = email or f"user-{uuid.uuid4().hex}@example.test"
    payload = {"name": "Teste", "email": email, "password": "Senha123", "accept_terms": True}
    response = await client.post("/api/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return {"email": email, "token": response.json()["access_token"]}
