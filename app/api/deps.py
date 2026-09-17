"""Dependências e infraestrutura de request cross-cutting.

get_current_user, o cache de escopo de request e o roteamento sem
response_model são usados por TODOS os domínios — não fazem sentido dentro de
um único domínio. A exceção às regras de dependência (core/shared nunca
importam de domínio) é `app.auth.service`: resolver o usuário da sessão exige
uma consulta que só o domínio auth conhece, então este módulo importa
especificamente daquele service.py, documentado também em
docs/architecture/overview.md.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, Request, Response, status
from fastapi.exceptions import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import OAuth2PasswordBearer
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.database import db_cursor, storage_available
from app.core.ephemeral import ip_rate_limit_fallback, revoked_token_hashes
from app.core.logging import audit_log, client_ip_hash
from app.core.security import create_access_token, token_hash
from app.core.signing import resolve_jwt_secret
from app.shared.dates import as_utc_datetime
from app.shared.serialization import normalize_row

logger = logging.getLogger("trevo")

AUTH_COOKIE_NAME = "trevo_access_token"
CSRF_COOKIE_NAME = "trevo_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
limiter = Limiter(key_func=get_remote_address)


# Dinheiro sai da API como número JSON, nunca string.
#
# As rotas anotam `-> dict` / `-> list[dict]`, e o FastAPI promove a anotação de
# retorno a response_model. Sob Pydantic v2 isso muda o serializador: Decimal
# passa a virar string ("2500.00" em vez de 2500). O frontend declara esses
# campos como number, então todo valor monetário chegava quebrado.
#
# As anotações são genéricas (dict), ou seja, não validam nada de útil — a
# resposta é montada à mão. Dispensar o response_model devolve o encoder padrão
# do FastAPI, que serializa Decimal como float.
class PlainDictRoute(APIRoute):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["response_model"] = None
        super().__init__(*args, **kwargs)


# Agregados caros (orçamento, metas) são pedidos várias vezes dentro da mesma
# request — /api/bootstrap sozinho pedia o orçamento 3x e as metas 2x. O cache
# vive só enquanto a request dura, então nunca serve dado velho entre requests.
_request_cache: ContextVar[dict | None] = ContextVar("request_cache", default=None)


def request_cached(key: tuple, factory):
    cache = _request_cache.get()
    if cache is None:
        return factory()
    if key not in cache:
        cache[key] = factory()
    return cache[key]


def get_jwt_secret() -> str:
    return resolve_jwt_secret()


def decode_token_metadata(token: str) -> tuple[str | None, datetime | None]:
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None, None
    user_id = str(claims.get("sub")) if claims.get("sub") else None
    expires_at = None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        expires_at = datetime.fromtimestamp(exp, UTC)
    return user_id, expires_at


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        max_age=settings.access_token_expire_hours * 3600,
        path="/",
        samesite="lax",
        secure=settings.is_production,
    )
    # Pair every session with a fresh CSRF token (double-submit).
    issue_csrf_cookie(response)


def token_response_body(request: Request, token: str) -> dict:
    """Corpo de resposta de login/registro (SEC-12).

    O cookie já foi setado por set_auth_cookie — o SPA nunca lê este campo,
    só o cookie. Devolver o token no corpo mesmo assim faz ele trafegar (e
    ser logado por proxies/DevTools) sem necessidade. Clientes de API que
    dependem de Bearer continuam recebendo o token normalmente; o SPA sinaliza
    que só precisa do cookie com o header X-Token-Response: omit.
    """
    if request.headers.get("x-token-response") == "omit":
        return {"token_type": "bearer"}  # nosec B105
    return {"access_token": token, "token_type": "bearer"}  # nosec B105


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", samesite="lax", secure=settings.is_production)
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", samesite="lax", secure=settings.is_production)


def issue_csrf_cookie(response: Response) -> str:
    # Readable by JS (not HttpOnly) so the SPA can echo it in the X-CSRF-Token
    # header — that echo is what proves same-origin (double-submit).
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,
        max_age=settings.access_token_expire_hours * 3600,
        path="/",
        samesite="lax",
        secure=settings.is_production,
    )
    return token


def revoke_token(token: str, user_id: str | None = None) -> None:
    current_hash = token_hash(token)
    revoked_token_hashes.add(current_hash)
    if not storage_available():
        return
    decoded_user_id, expires_at = decode_token_metadata(token)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO revoked_tokens (token_hash, user_id, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (token_hash)
                DO UPDATE SET revoked_at = NOW(), expires_at = EXCLUDED.expires_at
                """,
                (current_hash, user_id or decoded_user_id, expires_at),
            )
    except Exception:
        logger.exception("Failed to persist revoked token")


def is_token_revoked(token: str) -> bool:
    current_hash = token_hash(token)
    if current_hash in revoked_token_hashes:
        return True
    if not storage_available():
        return False
    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM revoked_tokens
                WHERE token_hash = %s
                  AND (expires_at IS NULL OR expires_at > NOW())
                LIMIT 1
                """,
                (current_hash,),
            )
            return cursor.fetchone() is not None
    except Exception:
        logger.exception("Failed to read revoked token state")
        return False


def get_current_user(
    response: Response,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
) -> dict:
    # Import local para evitar ciclo: app.auth.service importa deste módulo
    # (get_current_user é usado como Depends() nas rotas de auth também).
    from app.auth.service import get_user_by_id, public_user

    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido ou expirado.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    # SEC-07: só renova quando a autenticação veio do cookie (Bearer é
    # explícito — o cliente de API controla o próprio ciclo de vida do
    # token, e não haveria onde escrever um Set-Cookie de qualquer forma).
    used_cookie = token is None and cookie_token is not None
    token = token or cookie_token
    if not token:
        raise credentials_error
    if is_token_revoked(token):
        raise credentials_error
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[settings.jwt_algorithm])
        subject = payload.get("sub")
        if not subject:
            raise credentials_error
        user_uuid = UUID(str(subject))
        issued_at_claim = payload.get("iat")
        expires_at_claim = payload.get("exp")
    except (jwt.PyJWTError, ValueError):
        raise credentials_error from None

    user = get_user_by_id(str(user_uuid))
    if not user or not user["is_active"]:
        raise credentials_error
    password_changed_at = as_utc_datetime(user.get("password_changed_at"))
    if password_changed_at:
        if not isinstance(issued_at_claim, (int, float)):
            raise credentials_error
        issued_at = datetime.fromtimestamp(float(issued_at_claim), UTC)
        if issued_at < password_changed_at:
            raise credentials_error

    if used_cookie and isinstance(expires_at_claim, (int, float)):
        # Renovação deslizante: reemite o cookie quando falta menos de 25% da
        # validade. Uso contínuo nunca deixa a sessão chegar perto de
        # expirar; parada, expira em até ACCESS_TOKEN_EXPIRE_HOURS após o
        # último request. Não revoga o token antigo — ele já vale até o
        # próprio exp original, e revogar aqui derrubaria outras abas com o
        # cookie ainda não atualizado (Set-Cookie não é instantâneo entre
        # abas).
        total_seconds = settings.access_token_expire_hours * 3600
        remaining_seconds = expires_at_claim - datetime.now(UTC).timestamp()
        if total_seconds > 0 and remaining_seconds < total_seconds * 0.25:
            set_auth_cookie(response, create_access_token(user["id"]))

    return public_user(user)


def get_optional_current_user(
    response: Response,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
) -> dict | None:
    """Como get_current_user, mas devolve None em vez de 401 sem sessão.

    Usado por rotas que precisam se comportar diferente se a requisição já
    está autenticada, sem tornar a autenticação obrigatória — o link OAuth
    (?link=true) exige sessão; o authorize normal não.
    """
    try:
        return get_current_user(response, token=token, cookie_token=cookie_token)
    except HTTPException:
        return None


def enforce_ip_rate_limit(request: Request, scope: str, max_attempts: int, window_seconds: int) -> None:
    """Limite de tentativas por IP, persistido em Postgres.

    O Limiter do slowapi (ver `limiter` acima) usa armazenamento em memória
    de processo — verificado em runtime. Em serverless cada instância tem o
    próprio contador, então o `@limiter.limit` nas rotas sensíveis
    (cadastro, troca de senha, exclusão de conta, exportação de dados) é, na
    prática, decorativo. Esta função implementa uma janela deslizante simples
    por (scope, ip) na tabela `rate_limit_state`, com o mesmo desenho de
    `enforce_login_rate_limit`.

    Degrada com graça (fail-open): se o banco falhar, cai no fallback em
    memória do processo e nunca bloqueia a requisição por indisponibilidade
    de armazenamento — bloquear login/cadastro porque o banco piscou seria
    pior do que o problema que isto resolve.
    """
    ip_hash = client_ip_hash(request)
    if not ip_hash:
        return
    key = hashlib.sha256(f"{scope}:{ip_hash}".encode()).hexdigest()
    now_dt = datetime.now(UTC)

    if storage_available():
        try:
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    "SELECT window_start, count FROM rate_limit_state WHERE key_hash = %s",
                    (key,),
                )
                row = normalize_row(cursor.fetchone())
                window_start = as_utc_datetime(row.get("window_start")) if row else None
                if not row or not window_start or (now_dt - window_start).total_seconds() > window_seconds:
                    window_start = now_dt
                    count = 1
                else:
                    count = int(row["count"]) + 1

                cursor.execute(
                    """
                    INSERT INTO rate_limit_state (key_hash, window_start, count)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (key_hash)
                    DO UPDATE SET window_start = EXCLUDED.window_start, count = EXCLUDED.count
                    """,
                    (key, window_start, count),
                )
            if count > max_attempts:
                audit_log("rate_limited", None, {"scope": scope})
                raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente mais tarde.")
            return
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to enforce persisted rate limit for scope=%s", scope)

    # Banco indisponível ou fora de serviço: fallback em memória do processo.
    entry = ip_rate_limit_fallback.get(key)
    now = now_dt.timestamp()
    if not entry or now - float(entry.get("window_start") or 0) > window_seconds:
        entry = {"window_start": now, "count": 0}
    entry["count"] = int(entry["count"]) + 1
    ip_rate_limit_fallback[key] = entry
    if entry["count"] > max_attempts:
        audit_log("rate_limited", None, {"scope": scope, "fallback": True})
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente mais tarde.")


def rate_limit_handler(request: Request, exc: Exception) -> Response:
    if isinstance(exc, RateLimitExceeded):
        return _rate_limit_exceeded_handler(request, exc)
    raise exc


__all__ = [
    "AUTH_COOKIE_NAME",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "PlainDictRoute",
    "clear_auth_cookie",
    "decode_token_metadata",
    "enforce_ip_rate_limit",
    "get_current_user",
    "get_jwt_secret",
    "get_optional_current_user",
    "is_token_revoked",
    "issue_csrf_cookie",
    "limiter",
    "oauth2_scheme",
    "rate_limit_handler",
    "request_cached",
    "revoke_token",
    "set_auth_cookie",
    "token_response_body",
]
