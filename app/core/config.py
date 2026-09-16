from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Single source of truth for runtime configuration.

    Values are read from the environment at import time. Validation of the
    required secrets is intentionally deferred to ``require_*`` helpers so that
    ``import app.main`` keeps working in CI without a full environment.
    """

    environment: str = os.getenv("ENVIRONMENT", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_format: str = os.getenv("LOG_FORMAT", "text")

    database_url: str = os.getenv("DATABASE_URL", "")

    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "")
    jwt_algorithm: str = "HS256"
    # SEC-07: era 168h (7 dias) sem renovação — uma sessão continuava válida
    # por uma semana inteira mesmo parada. 24h + renovação deslizante (ver
    # get_current_user) equivale, na prática, a "nunca expira em uso
    # contínuo, expira em até 24h de inatividade" — mais seguro sem piorar
    # a experiência de quem usa o produto todo dia.
    access_token_expire_hours: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", "24"))

    allowed_origins_raw: str = os.getenv("ALLOWED_ORIGINS", "")
    trusted_hosts_raw: str = os.getenv("TRUSTED_HOSTS", "")

    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    supabase_avatars_bucket: str = os.getenv("SUPABASE_AVATARS_BUCKET", "avatars")

    # Secret/URL accessors read the environment at call time (falling back to the
    # import-time snapshot) so that runtime changes and test monkeypatching are
    # honored, matching the original dynamic behavior.

    @property
    def is_production(self) -> bool:
        return os.getenv("ENVIRONMENT", self.environment).lower() == "production"

    @property
    def is_serverless(self) -> bool:
        # Vercel sets ``VERCEL=1`` in the build and runtime environment.
        return bool(os.getenv("VERCEL"))

    @property
    def allowed_origins(self) -> list[str]:
        raw = os.getenv("ALLOWED_ORIGINS", self.allowed_origins_raw)
        origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
        if origins:
            return origins
        if self.is_production:
            return []
        return ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:3000"]

    @property
    def trusted_hosts(self) -> list[str]:
        """Hosts aceitos no header ``Host`` (SEC-06).

        Sem isto, ``app/oauth.py`` deriva o redirect do fluxo OAuth de
        ``request.base_url`` — que vem direto do ``Host`` da requisição, sem
        validação. Um ``Host`` forjado faz o servidor redirecionar para um
        domínio arbitrário depois do login (open redirect).

        Vazio por padrão: sem um valor configurado explicitamente não há como
        saber os hosts legítimos de cada ambiente sem risco de derrubar
        produção com um allowlist errado. Defina em produção com o(s)
        domínio(s) público(s), separados por vírgula.
        """
        raw = os.getenv("TRUSTED_HOSTS", self.trusted_hosts_raw)
        return [host.strip() for host in raw.split(",") if host.strip()]

    @property
    def supabase_configured(self) -> bool:
        return bool(self.effective_supabase_url and self.effective_supabase_service_role_key)

    @property
    def effective_supabase_url(self) -> str:
        return os.getenv("SUPABASE_URL", self.supabase_url)

    @property
    def effective_supabase_service_role_key(self) -> str:
        return os.getenv("SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_role_key)

    @property
    def effective_avatars_bucket(self) -> str:
        return os.getenv("SUPABASE_AVATARS_BUCKET", self.supabase_avatars_bucket)

    def require_database_url(self) -> str:
        value = os.getenv("DATABASE_URL", self.database_url).strip()
        if not value:
            raise RuntimeError("DATABASE_URL environment variable is required.")
        return value

    def require_jwt_secret(self) -> str:
        secret = os.getenv("JWT_SECRET_KEY", self.jwt_secret_key)
        if not secret:
            raise RuntimeError("JWT_SECRET_KEY environment variable is required.")
        if len(secret) < 32:
            raise RuntimeError("JWT_SECRET_KEY must have at least 32 characters.")
        return secret


settings = Settings()
