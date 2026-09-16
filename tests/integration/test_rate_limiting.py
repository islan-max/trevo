"""SEC-02: rate limit por IP persistido em Postgres.

O `Limiter` do slowapi usa `MemoryStorage` — verificado em runtime. Em
serverless cada instância tem o próprio contador, então o limite por IP nas
rotas sensíveis era, na prática, decorativo. Estes testes provam que a
camada persistida (`enforce_ip_rate_limit`, tabela `rate_limit_state`)
funciona independentemente da memória do slowapi.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DB_URL, reset_rate_limits

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")


@pytest.mark.asyncio
async def test_register_rate_limit_persists_across_memory_reset(client):
    base_payload = {"name": "Teste", "password": "Senha123", "accept_terms": True}

    for _ in range(3):
        email = f"user-{uuid.uuid4().hex}@example.test"
        response = await client.post("/api/auth/register", json={**base_payload, "email": email})
        assert response.status_code == 201, response.text

    # Simula a perda do estado em memória de uma instância serverless: zera
    # o limiter do slowapi. Se o bloqueio a seguir ainda ocorrer, é a camada
    # persistida em Postgres que está sustentando o limite, não a memória do
    # processo.
    reset_rate_limits()

    email = f"user-{uuid.uuid4().hex}@example.test"
    blocked = await client.post("/api/auth/register", json={**base_payload, "email": email})
    assert blocked.status_code == 429, blocked.text
