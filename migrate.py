from __future__ import annotations

import logging
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("trevo_migrate")
BASE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = BASE_DIR / "migrations"

BASELINE_VERSION = "0000_baseline"


def _is_already_provisioned(conn) -> bool:
    """Detecta um banco que já tem o schema (produção rodando há tempo), para
    registrar BASELINE_VERSION como aplicada sem reexecutar a DDL dela — ela é
    idempotente, mas inclui um DROP+ADD CONSTRAINT em transactions que toma
    ACCESS EXCLUSIVE e revalida a tabela inteira. `users` é a primeira tabela
    que ela cria; a presença dela já basta, porque nenhum banco chega a ter
    `users` sem essa DDL ter rodado por completo antes (era executada sem
    controle de versão a cada cold start em serverless)."""
    with conn.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'users')")
        return bool(cursor.fetchone()[0])


def _ensure_schema_migrations_table(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    conn.commit()


def backfill_baseline_if_provisioned(conn) -> None:
    _ensure_schema_migrations_table(conn)
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (BASELINE_VERSION,))
        already_recorded = cursor.fetchone() is not None

    if already_recorded or not _is_already_provisioned(conn):
        return

    logger.info("Banco já provisionado: registrando %s sem reexecutar a DDL.", BASELINE_VERSION)
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (BASELINE_VERSION,),
        )
    conn.commit()


def pending_migrations(conn) -> list[str]:
    """Lista, sem aplicar nada, as migrations em migrations/*.sql que ainda
    não estão registradas em schema_migrations."""
    versions = sorted(path.stem for path in MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else []
    if not versions:
        return []
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations')"
        )
        if not cursor.fetchone()[0]:
            return versions
        cursor.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cursor.fetchall()}
    return [version for version in versions if version not in applied]


def apply_versioned_migrations(conn) -> None:
    _ensure_schema_migrations_table(conn)

    if not MIGRATIONS_DIR.exists():
        return

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
            if cursor.fetchone():
                continue
            logger.info("Applying migration %s", path.name)
            cursor.execute(path.read_text(encoding="utf-8"))
            cursor.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
                (version,),
            )
        # Uma migration por transação: se a próxima falhar, o que já foi aplicado
        # continua registrado e o retry recomeça do ponto certo.
        conn.commit()


def run_migrations(conn) -> None:
    backfill_baseline_if_provisioned(conn)
    apply_versioned_migrations(conn)


def migrate() -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required.")

    conn = psycopg2.connect(database_url)
    try:
        run_migrations(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
    logger.info("Migra\u00e7\u00f5es aplicadas com sucesso.")

