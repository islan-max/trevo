"""Resolução do segredo de assinatura de tokens.

Ordem de precedência:

1. ``JWT_SECRET_KEY`` no ambiente — o caminho recomendado, e o único usado em
   testes e desenvolvimento.
2. Um segredo gerado pelo servidor e guardado na tabela ``app_secrets``.

O passo 2 existe porque um deploy sem a variável configurada não conseguia
assinar nada: login, cadastro e OAuth respondiam 500 e o app ficava inutilizável
mesmo com o banco de pé. Gerar e persistir uma vez resolve isso sem inventar um
segredo novo a cada cold start (o que invalidaria as sessões de todo mundo a
cada poucos minutos em serverless).
"""

from __future__ import annotations

import logging
import os
import secrets

from app.core.config import settings

logger = logging.getLogger("trevo.signing")

# Nome da chave em app_secrets, não um segredo — falso positivo do bandit
# (B105 casa qualquer string com "secret"/"password" no nome da variável).
JWT_SECRET_NAME = "jwt_secret_key"  # nosec B105
MIN_SECRET_LENGTH = 32

# Cache de processo: evita ir ao banco a cada assinatura de token.
_cached_secret: str | None = None


def _generate() -> str:
    return secrets.token_urlsafe(48)


def _load_or_create_from_db() -> str:
    # Import tardio: app.core.database importa app.core.config, e importar o
    # banco no topo daqui fecharia um ciclo.
    from app.core.database import db_cursor

    with db_cursor(commit=True) as cursor:
        # INSERT-then-SELECT sob ON CONFLICT: se duas instâncias subirem juntas,
        # ambas terminam lendo o mesmo valor — o primeiro que gravou.
        cursor.execute(
            """
            INSERT INTO app_secrets (name, value)
            VALUES (%s, %s)
            ON CONFLICT (name) DO NOTHING
            """,
            (JWT_SECRET_NAME, _generate()),
        )
        cursor.execute("SELECT value FROM app_secrets WHERE name = %s", (JWT_SECRET_NAME,))
        row = cursor.fetchone()

    if not row:
        raise RuntimeError("Não foi possível provisionar o segredo de assinatura.")
    return str(row["value"])


def resolve_jwt_secret() -> str:
    """Devolve o segredo de assinatura, provisionando-o se necessário."""
    global _cached_secret

    env_secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if env_secret:
        if len(env_secret) < MIN_SECRET_LENGTH:
            raise RuntimeError("JWT_SECRET_KEY must have at least 32 characters.")
        return env_secret

    if _cached_secret:
        return _cached_secret

    try:
        _cached_secret = _load_or_create_from_db()
    except Exception as exc:
        raise RuntimeError(
            "JWT_SECRET_KEY não está definida e o segredo não pôde ser provisionado no banco."
        ) from exc

    if settings.is_production:
        # SEC-05: o segredo fica em texto claro em app_secrets. Quem lê essa
        # tabela (dump, snapshot, painel do Supabase, service-role key
        # vazada) consegue forjar uma sessão para QUALQUER usuário — um
        # vetor mais amplo que o de leitura de dados comuns, e sem rotação
        # possível sem derrubar todas as sessões ativas de uma vez.
        logger.warning(
            "JWT_SECRET_KEY não está definida em produção — assinando com um segredo "
            "gerado e guardado em app_secrets (SEC-05). Defina JWT_SECRET_KEY assim que possível."
        )
    else:
        logger.info("Segredo de assinatura carregado da tabela app_secrets")
    return _cached_secret


def secret_source() -> str:
    """Informa a origem do segredo de assinatura sem provisionar nada.

    Usado por GET /api/health para tornar o estado do SEC-05 visível em
    qualquer ambiente (inclusive serverless, onde validate_runtime_config
    não roda). 'env' | 'database' | 'unresolved' (ainda não calculado neste
    processo — só acontece antes do primeiro token assinado/verificado).
    """
    if os.getenv("JWT_SECRET_KEY", "").strip():
        return "env"
    if _cached_secret:
        return "database"
    return "unresolved"


def reset_cache() -> None:
    """Descarta o segredo em cache (usado nos testes)."""
    global _cached_secret
    _cached_secret = None
