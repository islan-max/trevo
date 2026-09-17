from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import RedirectResponse
from psycopg2 import errors

from app.api.deps import (
    AUTH_COOKIE_NAME,
    PlainDictRoute,
    clear_auth_cookie,
    enforce_ip_rate_limit,
    get_current_user,
    get_optional_current_user,
    issue_csrf_cookie,
    limiter,
    oauth2_scheme,
    revoke_token,
    set_auth_cookie,
    token_response_body,
)
from app.auth.schemas import ChangePasswordPayload, DeleteAccountPayload, ProfilePayload, RegisterPayload
from app.auth.service import (
    clear_login_failures,
    delete_profile_photo_file,
    detect_profile_photo_extension,
    enforce_login_rate_limit,
    ensure_user_defaults_for_cursor,
    get_user_by_email,
    get_user_by_id,
    link_oauth_identity_to_user,
    public_user,
    record_login_failure,
    resolve_oauth_user,
    save_profile_payload,
)
from app.core import storage
from app.core.database import db_cursor
from app.core.logging import audit_log, client_ip_hash, email_hash
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.oauth import (
    OAUTH_PROVIDERS,
    OAUTH_STATE_COOKIE,
    build_authorize_redirect,
    fetch_oauth_profile,
    frontend_redirect,
    list_providers,
    set_request_origin,
)
from app.privacy.service import record_consent
from app.shared.serialization import normalize_row, require_row
from app.shared.validation import EMAIL_RE, clean_text, normalize_email

logger = logging.getLogger("trevo")

PROFILE_PHOTO_MAX_BYTES = 512 * 1024

router = APIRouter(route_class=PlainDictRoute)


@router.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("3 per 1 hour")
def register(request: Request, response: Response, payload: RegisterPayload) -> dict:
    enforce_ip_rate_limit(request, "register", max_attempts=3, window_seconds=3600)
    email = normalize_email(payload.email)
    name = clean_text(payload.name, "Nome", 100)
    validate_password_strength(payload.password)
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail="\u00c9 necess\u00e1rio aceitar a Pol\u00edtica de Privacidade e os Termos.")

    ip_hash = client_ip_hash(request)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO users (email, hashed_password, name)
                VALUES (%s, %s, %s)
                RETURNING id, email, name, avatar_url, send_monthly_summary, is_active,
                          auth_provider, oauth_subject, password_changed_at, created_at, updated_at
                """,
                (email, hash_password(payload.password), name),
            )
            user = require_row(normalize_row(cursor.fetchone()), "Usu\u00e1rio n\u00e3o criado.")
            ensure_user_defaults_for_cursor(cursor, user["id"])
            record_consent(cursor, user["id"], scope="terms_privacy", granted=True, ip_hash=ip_hash, channel="register")
    except errors.UniqueViolation:
        audit_log("user_register_failed", None, {"email_hash": email_hash(email), "reason": "duplicate"})
        raise HTTPException(status_code=400, detail="E-mail j\u00e1 cadastrado.") from None

    audit_log("user_registered", str(user["id"]), {"email_hash": email_hash(email)})
    token = create_access_token(user["id"])
    set_auth_cookie(response, token)
    return token_response_body(request, token)



@router.get("/api/auth/oauth/providers")
def oauth_providers(request: Request) -> dict:
    set_request_origin(str(request.base_url))
    return {"providers": list_providers()}



@router.get("/api/auth/oauth/{provider}/authorize")
def oauth_authorize(
    provider: str,
    request: Request,
    link: bool = False,
    current_user: dict | None = Depends(get_optional_current_user),
) -> RedirectResponse:
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Provedor OAuth não suportado.")
    set_request_origin(str(request.base_url))
    link_user_id = None
    if link:
        # SEC-04: vincular uma conta social exige que o dono já esteja
        # autenticado por senha — sem isso, qualquer um poderia "vincular"
        # a própria conta social à conta de outra pessoa sem confirmação.
        if not current_user:
            raise HTTPException(
                status_code=401, detail="Entre com sua senha antes de vincular uma conta social."
            )
        link_user_id = str(current_user["id"])
    return build_authorize_redirect(provider, link_user_id=link_user_id)



@router.get("/api/auth/oauth/{provider}/callback")
def oauth_callback(
    request: Request,
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    oauth_state_cookie: str | None = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
) -> RedirectResponse:
    set_request_origin(str(request.base_url))

    def redirect_and_clear(
        *, access_token: str | None = None, error_message: str | None = None, link_success: bool = False
    ) -> RedirectResponse:
        response = frontend_redirect(access_token=access_token, error=error_message, link_success=link_success)
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/oauth")
        return response

    if provider not in OAUTH_PROVIDERS:
        return redirect_and_clear(error_message="unsupported_provider")
    if error:
        logger.info("OAuth provider error provider=%s error=%s", provider, error)
        return redirect_and_clear(error_message=error_description or error)
    if not code or not state:
        return redirect_and_clear(error_message="missing_code")
    try:
        profile, state_payload = fetch_oauth_profile(provider, code, state, oauth_state_cookie)
        link_user_id = state_payload.get("link_user_id")
        if link_user_id:
            link_oauth_identity_to_user(link_user_id, profile)
            return redirect_and_clear(link_success=True)
        user = resolve_oauth_user(profile)
        audit_log("login_success_oauth", str(user["id"]), {"provider": provider, "email_hash": email_hash(user["email"])})
        token = create_access_token(user["id"])
        response = redirect_and_clear(access_token=token)
        set_auth_cookie(response, token)
        return response
    except HTTPException as exc:
        logger.info("OAuth callback failed provider=%s detail=%s", provider, exc.detail)
        return redirect_and_clear(error_message=str(exc.detail))
    except Exception:
        logger.exception("OAuth callback unexpected failure provider=%s", provider)
        return redirect_and_clear(error_message="oauth_failed")



@router.post("/api/auth/login")
@limiter.limit("5 per 15 minutes")
def login(
    request: Request,
    response: Response,
    email: str = Form(..., max_length=255),
    password: str = Form(..., max_length=72),
) -> dict:
    email_value = email.strip().lower()
    enforce_login_rate_limit(email_value)
    email_is_valid = len(email_value) <= 255 and bool(EMAIL_RE.match(email_value))
    user = get_user_by_email(email_value) if email_is_valid else None
    stored_hash = user["hashed_password"] if user else None
    hash_to_check = stored_hash if stored_hash else DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, hash_to_check)

    if not user or not password_ok or not user["is_active"]:
        record_login_failure(email_value)
        audit_log("login_failed", str(user["id"]) if user else None, {"email_hash": email_hash(email_value)})
        raise HTTPException(status_code=401, detail="E-mail ou senha inv\u00e1lidos.")

    clear_login_failures(email_value)
    audit_log("login_success", str(user["id"]), {"email_hash": email_hash(email_value)})
    token = create_access_token(user["id"])
    set_auth_cookie(response, token)
    return token_response_body(request, token)



@router.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)) -> dict:
    return current_user



@router.get("/api/auth/csrf")
def get_csrf(response: Response, current_user: dict = Depends(get_current_user)) -> dict:
    token = issue_csrf_cookie(response)
    return {"csrf_token": token}



@router.put("/api/auth/me")
def update_me(payload: ProfilePayload, current_user: dict = Depends(get_current_user)) -> dict:
    return save_profile_payload(payload, current_user)



@router.post("/api/auth/me")
def save_me(payload: ProfilePayload, current_user: dict = Depends(get_current_user)) -> dict:
    return save_profile_payload(payload, current_user)



@router.post("/api/auth/me/avatar")
@limiter.limit("12 per 1 hour")
async def upload_profile_photo(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    enforce_ip_rate_limit(request, "avatar_upload", max_attempts=12, window_seconds=3600)
    content = await file.read(PROFILE_PHOTO_MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Escolha uma imagem para enviar.")
    if len(content) > PROFILE_PHOTO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="A foto deve ter no máximo 512 KB.")

    extension = detect_profile_photo_extension(file.content_type, content)
    user_id = str(current_user["id"])
    normalized_type = (file.content_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    try:
        avatar_url = storage.store_avatar(user_id, content, extension, normalized_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido.") from None
    previous_avatar_url = current_user.get("avatar_url")

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE users
            SET avatar_url = %s
            WHERE id = %s
            RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                      auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            """,
            (avatar_url, user_id),
        )
        user = require_row(normalize_row(cursor.fetchone()), "Foto de perfil não atualizada.")

    delete_profile_photo_file(previous_avatar_url)
    audit_log("profile_photo_uploaded", user_id)
    return public_user(user)



@router.post("/api/auth/change-password")
@limiter.limit("3 per 1 hour")
def change_password(
    request: Request,
    payload: ChangePasswordPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    enforce_ip_rate_limit(request, "change_password", max_attempts=3, window_seconds=3600)
    user = get_user_by_id(current_user["id"])
    if not user or not user.get("hashed_password") or not verify_password(payload.current_password, user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Senha atual incorreta.")

    validate_password_strength(payload.new_password)
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE users
            SET hashed_password = %s, password_changed_at = date_trunc('second', NOW())
            WHERE id = %s
            """,
            (hash_password(payload.new_password), current_user["id"]),
        )
    audit_log("password_changed", current_user["id"])
    return {"ok": True}



@router.get("/api/auth/stats")
def auth_stats(current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM transactions WHERE user_id = %s) AS total_transactions,
              (SELECT COUNT(*) FROM categories WHERE user_id = %s) AS total_categories
            """,
            (user_id, user_id),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Estat\u00edsticas n\u00e3o encontradas.")
    return {
        "created_at": current_user["created_at"],
        "total_transactions": int(row["total_transactions"]),
        "total_categories": int(row["total_categories"]),
    }



@router.post("/api/auth/logout")
def logout(
    response: Response,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    current_user: dict = Depends(get_current_user),
) -> dict:
    token = token or cookie_token
    if not token:
        raise HTTPException(status_code=401, detail="Token inv\u00e1lido ou expirado.")
    revoke_token(token, current_user["id"])
    clear_auth_cookie(response)
    return {"message": "Sessão encerrada no servidor."}



@router.delete("/api/auth/me")
@limiter.limit("5 per 1 hour")
def delete_account(
    request: Request,
    response: Response,
    payload: DeleteAccountPayload,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """LGPD Art. 18, IV — direito de eliminação.

    Exige reautenticação por senha (contas com senha); as FKs ON DELETE CASCADE
    em user_id removem settings/categorias/transações/cartões/orçamentos/etc.
    """
    enforce_ip_rate_limit(request, "delete_account", max_attempts=5, window_seconds=3600)
    user = get_user_by_id(current_user["id"])
    if user and user.get("hashed_password") and (
        not payload.password or not verify_password(payload.password, user["hashed_password"])
    ):
        raise HTTPException(status_code=400, detail="Senha incorreta.")

    avatar_ref = user.get("avatar_url") if user else None
    deleted_user_id = current_user["id"]
    with db_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM users WHERE id = %s", (deleted_user_id,))

    if avatar_ref and not storage.remove_avatar(avatar_ref):
        # SEC-11: a conta já foi apagada; sem isto, a falha desaparecia e a
        # eliminação LGPD ficava incompleta sem ninguém saber. Best-effort
        # também aqui — se ATÉ o registro da pendência falhar, loga e segue
        # (a exclusão da conta em si não pode travar por causa do avatar).
        try:
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    "INSERT INTO pending_avatar_deletions (user_id, avatar_ref) VALUES (%s, %s)",
                    (deleted_user_id, avatar_ref),
                )
        except Exception:
            logger.exception("Failed to record pending avatar deletion for user_id=%s", deleted_user_id)
        audit_log("avatar_deletion_failed", deleted_user_id, {"avatar_ref": avatar_ref})

    active_token = token or cookie_token
    if active_token:
        revoke_token(active_token, current_user["id"])
    clear_auth_cookie(response)
    audit_log("account_deleted", current_user["id"])
    return {"deleted": True}

