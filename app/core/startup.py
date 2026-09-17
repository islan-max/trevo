from __future__ import annotations

import logging

from app.core.config import settings
from app.core.database import get_database_url
from app.core.signing import resolve_jwt_secret

logger = logging.getLogger("trevo")


def validate_runtime_config() -> None:
    get_database_url()
    resolve_jwt_secret()
    if settings.is_production:
        origins = settings.allowed_origins
        if not origins:
            logger.info("ALLOWED_ORIGINS is empty in production. Cross-origin browser requests are disabled.")
        if any("localhost" in origin or "127.0.0.1" in origin for origin in origins):
            raise RuntimeError("ALLOWED_ORIGINS de produção não deve apontar para localhost.")
        if "*" in origins:
            logger.warning("ALLOWED_ORIGINS is '*' in production. Use only during the first deploy and replace it with the public HTTPS URL.")
        if not settings.trusted_hosts:
            logger.warning(
                "TRUSTED_HOSTS não está definida em produção. O header Host não é "
                "validado, e o redirect do fluxo OAuth (app/oauth.py) confia nele "
                "sem checagem (SEC-06). Defina TRUSTED_HOSTS com o(s) domínio(s) "
                "público(s), separados por vírgula."
            )
