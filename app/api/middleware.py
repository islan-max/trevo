"""Middlewares HTTP registrados em main.py, na mesma ordem desta lista:
add_request_id, validate_content_type, csrf_protect, add_security_headers.

São funções simples (não decoradas com @app.middleware aqui, porque `app`
nasce em main.py) — main.py as registra com `app.middleware("http")(func)`.
"""
from __future__ import annotations

import logging
import secrets
import time
import uuid

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import AUTH_COOKIE_NAME, CSRF_COOKIE_NAME, CSRF_HEADER_NAME, _request_cache
from app.core import database as database_module
from app.core.config import settings
from app.core.database import close_request_scope, ensure_serverless_schema, open_request_scope

logger = logging.getLogger("trevo")

# State-changing endpoints reached before a session cookie exists (so no CSRF risk).
CSRF_EXEMPT_PATHS = {"/api/auth/login", "/api/auth/register", "/api/auth/csrf"}
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    _request_cache.set({})
    if not database_module._schema_checked and request.url.path.startswith("/api/"):
        # Roda antes de open_request_scope(): essa checagem usa sua própria
        # conexão de uso único (run_in_threadpool copia o contexto atual para
        # a thread, então uma conexão cacheada lá dentro nunca seria fechada
        # de volta no contexto da request).
        await run_in_threadpool(ensure_serverless_schema)
    open_request_scope()
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        close_request_scope()


async def validate_content_type(request: Request, call_next):
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if (
        request.method in ("POST", "PUT", "PATCH")
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/auth/login"
        and content_type not in ("application/json", "multipart/form-data", "application/x-www-form-urlencoded", "")
    ):
        return JSONResponse(
            {"detail": "Content-Type inválido."},
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    return await call_next(request)


async def csrf_protect(request: Request, call_next):
    # Double-submit CSRF: for state-changing /api requests authenticated by the
    # session cookie (not a Bearer token), require the X-CSRF-Token header to
    # match the trevo_csrf cookie. Bearer/API clients are exempt, and requests
    # before login (no auth cookie yet) are naturally exempt.
    path = request.url.path
    if (
        request.method not in CSRF_SAFE_METHODS
        and path.startswith("/api/")
        and path not in CSRF_EXEMPT_PATHS
        and not path.startswith("/api/auth/oauth/")
    ):
        has_bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        auth_cookie = request.cookies.get(AUTH_COOKIE_NAME)
        if auth_cookie and not has_bearer:
            cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
            header_csrf = request.headers.get(CSRF_HEADER_NAME)
            if not cookie_csrf or not header_csrf or not secrets.compare_digest(cookie_csrf, header_csrf):
                return JSONResponse({"detail": "CSRF token inválido ou ausente."}, status_code=403)
    return await call_next(request)


async def add_security_headers(request: Request, call_next):
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Request failed method=%s path=%s",
            request.method,
            request.url.path,
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        raise

    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "Request completed method=%s path=%s status=%s duration_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # 'unsafe-inline' on script-src is required by Next.js static export (inline
    # hydration bootstrap, no nonce support in export mode). External script/font
    # origins are dropped since the frontend self-hosts everything.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "upgrade-insecure-requests"
    )
    return response
