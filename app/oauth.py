from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt

from app.core.signing import resolve_jwt_secret

logger = logging.getLogger("trevo.oauth")

OAUTH_STATE_TTL_SECONDS = 10 * 60
OAUTH_STATE_COOKIE = "trevo_oauth_state"
OAUTH_STATE_ALG = "HS256"
OAUTH_PROVIDERS = ("google", "github", "facebook")


@dataclass(frozen=True)
class OAuthProviderConfig:
    provider: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    profile_loader: Callable[[str, dict[str, str]], dict[str, str]]


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


# Origem da request em curso, preenchida pelas rotas de OAuth. Em deploy de
# mesma origem (Vercel: front e API no mesmo domínio) ela é a fonte mais
# confiável da URL pública — evita depender de env var que ninguém lembra de
# atualizar quando o domínio muda.
_request_origin: ContextVar[str] = ContextVar("oauth_request_origin", default="")


def set_request_origin(origin: str) -> None:
    _request_origin.set(origin.rstrip("/"))


def oauth_redirect_base() -> str:
    """Base pública da API, para montar o redirect_uri do provedor."""
    explicit = _env("OAUTH_REDIRECT_BASE_URL").rstrip("/")
    if explicit:
        return explicit
    return _request_origin.get()


def oauth_frontend_callback_url() -> str:
    """Para onde o usuário volta depois que o provedor responde.

    Antes, sem OAUTH_FRONTEND_CALLBACK_URL nem ALLOWED_ORIGINS, isto caía em
    ``http://localhost:3000`` — em produção o login social terminava jogando o
    usuário para a máquina dele. Em deploy de mesma origem o destino certo é a
    própria origem da request.
    """
    explicit = _env("OAUTH_FRONTEND_CALLBACK_URL").rstrip("/")
    if explicit:
        return explicit
    origins = [origin.strip() for origin in _env("ALLOWED_ORIGINS").split(",") if origin.strip()]
    if origins:
        return f"{origins[0].rstrip('/')}/oauth/callback"
    origin = _request_origin.get()
    if origin:
        return f"{origin}/oauth/callback"
    return "http://localhost:3000/oauth/callback"


def provider_callback_url(provider: str) -> str:
    base = oauth_redirect_base()
    if not base:
        raise HTTPException(status_code=503, detail="OAuth não configurado (origem indisponível).")
    return f"{base}/api/auth/oauth/{provider}/callback"


def _google_profile(access_token: str, _: dict[str, str]) -> dict[str, str]:
    response = httpx.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15.0,
    )
    response.raise_for_status()
    data = response.json()
    email = str(data.get("email") or "").strip().lower()
    if not email or not data.get("email_verified", True):
        raise HTTPException(status_code=400, detail="E-mail do Google não verificado.")
    return {
        "email": email,
        "name": str(data.get("name") or email.split("@")[0]),
        "subject": str(data.get("sub") or ""),
    }


def _github_profile(access_token: str, _: dict[str, str]) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    user_response = httpx.get("https://api.github.com/user", headers=headers, timeout=15.0)
    user_response.raise_for_status()
    user = user_response.json()
    email = str(user.get("email") or "").strip().lower()
    if not email:
        emails_response = httpx.get("https://api.github.com/user/emails", headers=headers, timeout=15.0)
        emails_response.raise_for_status()
        for entry in emails_response.json():
            if entry.get("primary") and entry.get("verified"):
                email = str(entry.get("email") or "").strip().lower()
                break
        if not email:
            for entry in emails_response.json():
                if entry.get("verified"):
                    email = str(entry.get("email") or "").strip().lower()
                    break
    if not email:
        raise HTTPException(status_code=400, detail="GitHub não retornou e-mail verificado.")
    return {
        "email": email,
        "name": str(user.get("name") or user.get("login") or email.split("@")[0]),
        "subject": str(user.get("id") or ""),
    }


def _facebook_profile(access_token: str, _: dict[str, str]) -> dict[str, str]:
    response = httpx.get(
        "https://graph.facebook.com/v19.0/me",
        params={"fields": "id,name,email", "access_token": access_token},
        timeout=15.0,
    )
    response.raise_for_status()
    data = response.json()
    email = str(data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=400,
            detail="Facebook não retornou e-mail. Verifique permissões do app e conta vinculada.",
        )
    return {
        "email": email,
        "name": str(data.get("name") or email.split("@")[0]),
        "subject": str(data.get("id") or ""),
    }


def provider_config(provider: str) -> OAuthProviderConfig | None:
    if provider == "google":
        client_id = _env("GOOGLE_CLIENT_ID")
        client_secret = _env("GOOGLE_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None
        return OAuthProviderConfig(
            provider="google",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",  # nosec B106
            scopes=("openid", "email", "profile"),
            profile_loader=_google_profile,
        )
    if provider == "github":
        client_id = _env("GITHUB_CLIENT_ID")
        client_secret = _env("GITHUB_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None
        return OAuthProviderConfig(
            provider="github",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",  # nosec B106
            scopes=("read:user", "user:email"),
            profile_loader=_github_profile,
        )
    if provider == "facebook":
        client_id = _env("FACEBOOK_CLIENT_ID")
        client_secret = _env("FACEBOOK_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None
        return OAuthProviderConfig(
            provider="facebook",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url="https://www.facebook.com/v19.0/dialog/oauth",
            token_url="https://graph.facebook.com/v19.0/oauth/access_token",  # nosec B106
            scopes=("email", "public_profile"),
            profile_loader=_facebook_profile,
        )
    return None


def list_providers() -> dict[str, dict[str, bool]]:
    base_ready = bool(oauth_redirect_base())
    payload: dict[str, dict[str, bool]] = {}
    for provider in OAUTH_PROVIDERS:
        config = provider_config(provider)
        payload[provider] = {
            "enabled": bool(config and base_ready),
            "configured": bool(config),
            "redirect_ready": base_ready,
        }
    return payload


def create_oauth_state(provider: str, link_user_id: str | None = None) -> str:
    """Stateless, signed OAuth state.

    The provider and expiry are carried inside a signed token (no server-side
    storage), so authorize and callback can be served by different serverless
    invocations. The same value is mirrored in an HttpOnly cookie for CSRF
    (double-submit) protection.

    ``link_user_id``, when present, marks this authorization as a request to
    link the provider to an ALREADY authenticated account (SEC-04) rather
    than a login — the callback branches on it instead of resolving/creating
    a user by e-mail.
    """
    now = int(time.time())
    payload = {
        "provider": provider,
        "iat": now,
        "exp": now + OAUTH_STATE_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(16),
    }
    if link_user_id:
        payload["link_user_id"] = link_user_id
    return jwt.encode(payload, resolve_jwt_secret(), algorithm=OAUTH_STATE_ALG)


def consume_oauth_state(state: str, provider: str, cookie_state: str | None) -> dict:
    # Double-submit: the state in the URL must match the one in the cookie.
    if not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(status_code=400, detail="State OAuth inválido ou expirado.")
    try:
        payload = jwt.decode(state, resolve_jwt_secret(), algorithms=[OAUTH_STATE_ALG])
    except JWTError:
        raise HTTPException(status_code=400, detail="State OAuth inválido ou expirado.") from None
    if payload.get("provider") != provider:
        raise HTTPException(status_code=400, detail="State OAuth inválido ou expirado.")
    return payload


def build_authorize_redirect(provider: str, link_user_id: str | None = None) -> RedirectResponse:
    config = provider_config(provider)
    if not config or not oauth_redirect_base():
        raise HTTPException(status_code=503, detail=f"Login com {provider} indisponível no momento.")
    state = create_oauth_state(provider, link_user_id=link_user_id)
    params = {
        "client_id": config.client_id,
        "redirect_uri": provider_callback_url(provider),
        "response_type": "code",
        "scope": " ".join(config.scopes),
        "state": state,
    }
    if provider == "github":
        params["allow_signup"] = "true"
    response = RedirectResponse(f"{config.authorize_url}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state,
        httponly=True,
        max_age=OAUTH_STATE_TTL_SECONDS,
        path="/api/auth/oauth",
        samesite="lax",
        secure=_env("ENVIRONMENT").lower() == "production",
    )
    return response


def exchange_code_for_token(config: OAuthProviderConfig, code: str) -> str:
    payload = {
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "code": code,
        "redirect_uri": provider_callback_url(config.provider),
        "grant_type": "authorization_code",
    }
    headers = {"Accept": "application/json"}
    if config.provider == "github":
        headers["Accept"] = "application/json"
    response = httpx.post(config.token_url, data=payload, headers=headers, timeout=15.0)
    response.raise_for_status()
    data = response.json()
    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Provedor OAuth não retornou token de acesso.")
    return str(access_token)


def fetch_oauth_profile(provider: str, code: str, state: str, cookie_state: str | None) -> tuple[dict[str, str], dict]:
    """Troca o code pelo perfil OAuth e devolve, junto, o payload do state.

    O payload carrega ``link_user_id`` quando esta autorização começou como
    um pedido de vínculo (ver create_oauth_state) — o callback usa isso para
    decidir entre logar/criar um usuário ou vincular o provedor à conta já
    autenticada que iniciou o fluxo.
    """
    state_payload = consume_oauth_state(state, provider, cookie_state)
    config = provider_config(provider)
    if not config:
        raise HTTPException(status_code=503, detail=f"Login com {provider} indisponível.")
    access_token = exchange_code_for_token(config, code)
    profile = config.profile_loader(access_token, {})
    if not profile.get("subject"):
        raise HTTPException(status_code=400, detail="Identidade OAuth inválida.")
    profile["provider"] = provider
    return profile, state_payload


def frontend_redirect(
    access_token: str | None = None, error: str | None = None, link_success: bool = False
) -> RedirectResponse:
    base = oauth_frontend_callback_url()
    if link_success:
        return RedirectResponse(f"{base}?linked=1", status_code=302)
    if access_token:
        return RedirectResponse(f"{base}?session=1", status_code=302)
    query = urlencode({"error": error or "oauth_failed"})
    return RedirectResponse(f"{base}?{query}", status_code=302)
