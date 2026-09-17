from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.core.config import settings
from migrate import pending_migrations

logger = logging.getLogger("trevo.database")

# In container mode a process-wide pool is reused across requests. In serverless
# mode (Vercel) there is no long-lived process to hold a pool, so each request
# opens a short connection against the Supabase transaction pooler (port 6543)
# and closes it. DATABASE_URL must point at the pooler when running on Vercel.
_db_pool: ThreadedConnectionPool | None = None

# PERF-04: sem isso, cada chamada a connection() em modo serverless abria uma
# conexão nova — um request que faz 10 queries abria 10 conexões contra o
# pooler. `_NOT_IN_REQUEST` distingue "fora de uma request" (script de
# migração, por exemplo, onde o comportamento antigo de abrir-e-fechar
# continua valendo) de "dentro de uma request, conexão ainda não aberta"
# (onde a primeira conexão aberta é guardada e reaproveitada até o fim dela).
_NOT_IN_REQUEST = object()
_request_connection: ContextVar[object] = ContextVar("request_connection", default=_NOT_IN_REQUEST)


def open_request_scope() -> None:
    """Serverless only: marca o início de uma request para connection() cachear a 1ª conexão."""
    if settings.is_serverless:
        _request_connection.set(None)


def close_request_scope() -> None:
    """Fecha a conexão compartilhada da request (se alguma tiver sido aberta)."""
    conn = _request_connection.get()
    if conn is not None and conn is not _NOT_IN_REQUEST:
        conn.close()
    _request_connection.set(_NOT_IN_REQUEST)


def get_database_url() -> str:
    return settings.require_database_url()


def init_db_pool() -> None:
    global _db_pool
    if settings.is_serverless:
        return
    if _db_pool is not None:
        return
    _db_pool = ThreadedConnectionPool(minconn=2, maxconn=10, dsn=get_database_url())
    logger.info("Database pool initialized (container mode)")


def close_db_pool() -> None:
    global _db_pool
    if _db_pool is not None:
        _db_pool.closeall()
        _db_pool = None


def storage_available() -> bool:
    """Whether persistent Postgres storage is reachable.

    Serverless connects per request, so availability is decided by config; in
    container mode it depends on the pool having been initialized at startup.
    """
    if settings.is_serverless:
        try:
            return bool(settings.require_database_url())
        except RuntimeError:
            return False
    return _db_pool is not None


@contextmanager
def connection() -> Iterator[psycopg2.extensions.connection]:
    """Yield a raw connection (used by migrations and multi-statement work)."""
    if settings.is_serverless:
        current = _request_connection.get()
        if current is not None and current is not _NOT_IN_REQUEST:
            yield current
            return
        conn = psycopg2.connect(get_database_url())
        if current is None:
            # Dentro do escopo de uma request (open_request_scope já rodou):
            # guarda essa conexão para as próximas chamadas a connection().
            _request_connection.set(conn)
            yield conn
            return
        try:
            yield conn
        finally:
            conn.close()
        return

    if _db_pool is None:
        raise RuntimeError("Database pool is not initialized.")
    conn = _db_pool.getconn()
    try:
        yield conn
    finally:
        _db_pool.putconn(conn)


@contextmanager
def db_cursor(commit: bool = False):
    with connection() as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                yield cursor
            if commit:
                conn.commit()
        except Exception:
            if commit:
                conn.rollback()
            raise


# OPS-01/CI-03: em serverless, isto já aplicou migrations a cada cold start —
# incluindo DDL não-idempotente em custo (um DROP+ADD CONSTRAINT que toma
# ACCESS EXCLUSIVE em transactions), serializada por advisory lock entre
# instâncias concorrentes. Migrations agora rodam uma vez no build da Vercel
# (buildCommand em vercel.json chama `python migrate.py`); aqui só se
# VERIFICA se o banco está em dia, sem aplicar nada.
_schema_checked = False


def ensure_serverless_schema() -> None:
    global _schema_checked
    if _schema_checked or not settings.is_serverless:
        return
    _schema_checked = True  # uma checagem por processo, mesmo se falhar
    try:
        with connection() as conn:
            pending = pending_migrations(conn)
        if pending:
            logger.warning("Migrations pendentes no banco: %s. Rode `python migrate.py` no deploy.", pending)
        else:
            logger.info("Schema em dia.")
    except Exception:
        logger.exception("Falha ao verificar o schema; servindo assim mesmo")
