"""BP-08 (OPS-01): migrate.py deixou de rodar o schema inteiro sem controle de
versão a cada chamada — ele agora vive em migrations/0000_baseline.sql, uma
migration versionada como qualquer outra. Estes testes cobrem os três
cenários que run_migrations precisa acertar: banco vazio, banco já
provisionado (schema existe, mas 0000 nunca foi registrada) e reaplicação
idempotente. Usam um banco descartável à parte para não interferir no banco
principal de teste, que os outros arquivos de integração compartilham.
"""

from __future__ import annotations

import re

import psycopg2
import pytest

import migrate
from tests.conftest import TEST_DB_URL

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL is not configured")

_SCRATCH_DB_NAME = "trevo_migrate_scratch"


def _dsn_for_database(name: str) -> str:
    return re.sub(r"/[^/?]+(\?.*)?$", f"/{name}", TEST_DB_URL)


def _create_scratch_database() -> None:
    conn = psycopg2.connect(_dsn_for_database("postgres"))
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB_NAME}" WITH (FORCE)')
            cursor.execute(f'CREATE DATABASE "{_SCRATCH_DB_NAME}"')
    finally:
        conn.close()


def _drop_scratch_database() -> None:
    conn = psycopg2.connect(_dsn_for_database("postgres"))
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB_NAME}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def scratch_db():
    _create_scratch_database()
    conn = psycopg2.connect(_dsn_for_database(_SCRATCH_DB_NAME))
    try:
        yield conn
    finally:
        conn.close()
        _drop_scratch_database()


def test_run_migrations_creates_full_schema_on_empty_database(scratch_db):
    migrate.run_migrations(scratch_db)

    with scratch_db.cursor() as cursor:
        cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
        applied = [row[0] for row in cursor.fetchall()]
    assert migrate.BASELINE_VERSION in applied
    assert migrate.pending_migrations(scratch_db) == []

    with scratch_db.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.users')")
        assert cursor.fetchone()[0] == "users"
        cursor.execute("SELECT to_regclass('public.transactions')")
        assert cursor.fetchone()[0] == "transactions"


def test_run_migrations_is_idempotent(scratch_db):
    migrate.run_migrations(scratch_db)
    with scratch_db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM schema_migrations")
        first_count = cursor.fetchone()[0]

    migrate.run_migrations(scratch_db)
    with scratch_db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM schema_migrations")
        second_count = cursor.fetchone()[0]

    assert first_count == second_count


def test_backfill_baseline_registers_without_running_ddl_on_provisioned_database(scratch_db):
    """Simula um banco já provisionado antes de existir controle de versão
    para o schema: a DDL já rodou (por fora do migrate.py, aqui), mas
    schema_migrations nunca ouviu falar de 0000_baseline. O backfill precisa
    registrar a versão sem tentar rodar a DDL de novo."""
    baseline_sql = (migrate.MIGRATIONS_DIR / "0000_baseline.sql").read_text(encoding="utf-8")
    with scratch_db.cursor() as cursor:
        cursor.execute(baseline_sql)
    scratch_db.commit()

    migrate.backfill_baseline_if_provisioned(scratch_db)

    with scratch_db.cursor() as cursor:
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (migrate.BASELINE_VERSION,))
        assert cursor.fetchone() is not None


def test_pending_migrations_lists_missing_versions_without_applying(scratch_db):
    migrate.run_migrations(scratch_db)
    with scratch_db.cursor() as cursor:
        cursor.execute("DELETE FROM schema_migrations WHERE version = '0016_pending_avatar_deletions'")
    scratch_db.commit()

    pending = migrate.pending_migrations(scratch_db)

    assert "0016_pending_avatar_deletions" in pending
    with scratch_db.cursor() as cursor:
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '0016_pending_avatar_deletions'")
        assert cursor.fetchone() is None
