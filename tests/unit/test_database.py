"""PERF-04: em modo serverless, connection() abria uma conexão nova a cada
chamada — um request com N queries abria N conexões contra o pooler. Estes
testes não precisam de Postgres de verdade: psycopg2.connect é substituído
por um fake que só conta quantas vezes foi chamado."""

from __future__ import annotations

import app.core.database as database_module


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _patch_connect(monkeypatch):
    created: list[_FakeConnection] = []

    def fake_connect(_dsn):
        conn = _FakeConnection()
        created.append(conn)
        return conn

    monkeypatch.setattr(database_module.psycopg2, "connect", fake_connect)
    monkeypatch.setattr(database_module, "get_database_url", lambda: "postgresql://fake")
    return created


def test_serverless_request_scope_reuses_one_connection(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    created = _patch_connect(monkeypatch)

    database_module.open_request_scope()
    try:
        with database_module.connection() as first:
            pass
        with database_module.connection() as second:
            pass
        with database_module.connection() as third:
            pass

        assert first is second is third
        assert len(created) == 1
        assert not created[0].closed
    finally:
        database_module.close_request_scope()

    assert created[0].closed


def test_serverless_without_request_scope_opens_and_closes_each_time(monkeypatch):
    """Fora de uma request (ex.: script de migração), o comportamento antigo
    de abrir-e-fechar por chamada continua valendo."""
    monkeypatch.setenv("VERCEL", "1")
    created = _patch_connect(monkeypatch)

    with database_module.connection():
        pass
    with database_module.connection():
        pass

    assert len(created) == 2
    assert all(conn.closed for conn in created)


def test_serverless_request_scope_isolated_between_requests(monkeypatch):
    """A conexão de uma request não vaza para a próxima."""
    monkeypatch.setenv("VERCEL", "1")
    created = _patch_connect(monkeypatch)

    database_module.open_request_scope()
    with database_module.connection() as conn_a:
        pass
    database_module.close_request_scope()

    database_module.open_request_scope()
    with database_module.connection() as conn_b:
        pass
    database_module.close_request_scope()

    assert conn_a is not conn_b
    assert len(created) == 2
    assert all(conn.closed for conn in created)
