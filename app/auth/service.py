from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from psycopg2 import errors

from app.auth.schemas import ProfilePayload
from app.core import storage
from app.core.database import db_cursor, storage_available
from app.core.ephemeral import login_failures
from app.core.logging import audit_log, email_hash
from app.core.security import hash_password
from app.shared.dates import as_utc_datetime
from app.shared.serialization import normalize_row, require_row
from app.shared.validation import clean_text, normalize_email, validate_optional_url

LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5

PROFILE_PHOTO_ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

DEFAULT_CATEGORIES: list[tuple[str, str, str, str, int, str]] = [
    ("Salário", "income", "#2E9D5B", "\U0001f4bc", 1, "income"),
    ("Freelance", "income", "#4FB877", "\U0001f9e0", 1, "income"),
    ("Investimentos", "income", "#7FD199", "\U0001f4c8", 1, "investment"),
    ("Moradia", "expense", "#D9A441", "\U0001f3e0", 1, "other"),
    ("Alimentação", "expense", "#E4884A", "\U0001f37d️", 1, "other"),
    ("Mercado", "expense", "#C97B9E", "\U0001f6d2", 1, "other"),
    ("Transporte", "expense", "#4E8FBF", "\U0001f68c", 1, "other"),
    ("Saúde", "expense", "#D1495B", "\U0001f48a", 1, "other"),
    ("Educação", "expense", "#8B7BC4", "\U0001f4da", 1, "other"),
    ("Assinaturas", "expense", "#4CA9A0", "\U0001f4fa", 1, "other"),
    ("Lazer", "expense", "#E0658A", "\U0001f3ae", 1, "other"),
    ("Contas", "expense", "#7A8B99", "\U0001f4a1", 1, "other"),
    ("Reserva", "expense", "#1F8049", "\U0001f4b0", 1, "reserve"),
    ("Pets", "expense", "#B08968", "\U0001f436", 1, "other"),
    ("Presentes", "expense", "#E07A5F", "\U0001f381", 1, "other"),
    ("Outros", "expense", "#96A5A0", "\U0001f4cc", 1, "other"),
]

logger = logging.getLogger("trevo")


def get_user_by_email(email: str) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                   auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            FROM users
            WHERE email = %s
            """,
            (email,),
        )
        return normalize_row(cursor.fetchone())



def get_user_by_oauth(provider: str, subject: str) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                   auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            FROM users
            WHERE auth_provider = %s AND oauth_subject = %s
            """,
            (provider, subject),
        )
        return normalize_row(cursor.fetchone())



def get_user_by_id(user_id: str) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                   auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        )
        return normalize_row(cursor.fetchone())



def oauth_only_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(48))



def resolve_oauth_user(profile: dict[str, str]) -> dict:
    provider = profile["provider"]
    subject = profile["subject"]
    email = normalize_email(profile["email"])
    name = clean_text(profile.get("name") or email.split("@")[0], "Nome", 100)

    existing_oauth = get_user_by_oauth(provider, subject)
    if existing_oauth:
        if not existing_oauth["is_active"]:
            raise HTTPException(status_code=403, detail="Conta desativada.")
        return existing_oauth

    by_email = get_user_by_email(email)
    if by_email:
        if not by_email["is_active"]:
            raise HTTPException(status_code=403, detail="Conta desativada.")
        if by_email.get("oauth_subject") and (
            by_email.get("auth_provider") != provider or str(by_email.get("oauth_subject")) != subject
        ):
            raise HTTPException(status_code=409, detail="E-mail já vinculado a outro provedor social.")
        if by_email.get("hashed_password") and not by_email.get("oauth_subject"):
            # SEC-04: a conta tem senha própria e nunca foi vinculada a
            # nenhum provedor social — vincular automaticamente aqui
            # transferiria a segurança dela inteiramente para a política de
            # verificação de e-mail do provedor OAuth, sem confirmação do
            # dono da conta. A vinculação intencional passa por
            # /api/auth/oauth/{provider}/authorize?link=true, autenticado.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Já existe uma conta com este e-mail. Entre com sua senha e "
                    "vincule o login social pelo seu perfil."
                ),
            )
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                UPDATE users
                SET auth_provider = %s, oauth_subject = %s, name = COALESCE(NULLIF(name, ''), %s)
                WHERE id = %s
                RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                          auth_provider, oauth_subject, password_changed_at, created_at, updated_at
                """,
                (provider, subject, name, by_email["id"]),
            )
            return require_row(normalize_row(cursor.fetchone()), "Usuário não encontrado.")

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO users (email, hashed_password, name, auth_provider, oauth_subject)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                          auth_provider, oauth_subject, password_changed_at, created_at, updated_at
                """,
                (email, oauth_only_password_hash(), name, provider, subject),
            )
            user = require_row(normalize_row(cursor.fetchone()), "Usuário não criado.")
            ensure_user_defaults_for_cursor(cursor, user["id"])
    except errors.UniqueViolation:
        linked = get_user_by_oauth(provider, subject) or get_user_by_email(email)
        if linked:
            return linked
        raise HTTPException(status_code=409, detail="Não foi possível vincular conta social.") from None

    audit_log("user_registered_oauth", str(user["id"]), {"provider": provider, "email_hash": email_hash(email)})
    return user



def ensure_user_defaults_for_cursor(cursor, user_id: str) -> None:
    cursor.execute(
        """
        INSERT INTO settings (id, user_id, monthly_income, daily_goal, reserve_amount, currency)
        VALUES (1, %s, 0, 0, 0, 'BRL')
        ON CONFLICT (user_id, id) DO NOTHING
        """,
        (user_id,),
    )
    cursor.executemany(
        """
        INSERT INTO categories (user_id, name, type, color, icon, is_default, role)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, name) DO NOTHING
        """,
        [
            (user_id, name, type_name, color, icon, is_default, role)
            for name, type_name, color, icon, is_default, role in DEFAULT_CATEGORIES
        ],
    )



def ensure_user_defaults(user_id: str) -> None:
    with db_cursor(commit=True) as cursor:
        ensure_user_defaults_for_cursor(cursor, user_id)



def public_user(user: dict) -> dict:
    return {
        "id": str(user["id"]),
        "email": user["email"],
        "name": user["name"],
        "avatar_url": storage.resolve_avatar_url(user.get("avatar_url")),
        "send_monthly_summary": bool(user.get("send_monthly_summary", False)),
        "is_active": bool(user["is_active"]),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        # Exposto para o perfil mostrar qual provedor social já está
        # vinculado à conta — nunca sensível, é o próprio usuário lendo.
        "auth_provider": user.get("auth_provider"),
    }



def link_oauth_identity_to_user(user_id: str, profile: dict[str, str]) -> None:
    """Vincula um provedor social à conta JÁ AUTENTICADA que iniciou o pedido.

    Ao contrário de resolve_oauth_user (usado no login), esta função nunca
    decide por conta própria a QUEM vincular — o usuário já está
    identificado pela sessão que abriu o fluxo (ver oauth_authorize com
    link=true), fechando o ponto de SEC-04.
    """
    provider = profile["provider"]
    subject = profile["subject"]
    existing = get_user_by_oauth(provider, subject)
    if existing and str(existing["id"]) != str(user_id):
        raise HTTPException(status_code=409, detail="Esta conta social já está vinculada a outro usuário.")
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET auth_provider = %s, oauth_subject = %s WHERE id = %s",
                (provider, subject, user_id),
            )
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Esta conta social já está vinculada a outro usuário.") from None
    audit_log("oauth_linked", user_id, {"provider": provider})



def save_profile_payload(payload: ProfilePayload, current_user: dict) -> dict:
    user_id = current_user["id"]
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    name = current_user["name"]
    previous_avatar_url = current_user.get("avatar_url")
    avatar_url = previous_avatar_url
    send_monthly_summary = bool(current_user.get("send_monthly_summary", False))

    if "name" in fields_set:
        if payload.name is None:
            raise HTTPException(status_code=400, detail="Nome \u00e9 obrigat\u00f3rio.")
        name = clean_text(payload.name, "Nome", 100)

    if "avatar_url" in fields_set:
        avatar_url = None
    if "avatar_url" in fields_set and payload.avatar_url is not None:
        avatar_url = validate_optional_url(payload.avatar_url, "URL do avatar")

    if "send_monthly_summary" in fields_set and payload.send_monthly_summary is not None:
        send_monthly_summary = bool(payload.send_monthly_summary)

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE users
            SET name = %s, avatar_url = %s, send_monthly_summary = %s
            WHERE id = %s
            RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                      auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            """,
            (name, avatar_url, send_monthly_summary, user_id),
        )
        user = require_row(normalize_row(cursor.fetchone()), "Usu\u00e1rio n\u00e3o atualizado.")
    if "avatar_url" in fields_set and avatar_url is None:
        delete_profile_photo_file(previous_avatar_url)
    return public_user(user)



def login_failure_key(email: str) -> str:
    return email_hash(email.strip().lower())



def enforce_login_rate_limit(email: str) -> None:
    key = login_failure_key(email)
    now_dt = datetime.now(UTC)
    now = now_dt.timestamp()
    if storage_available():
        try:
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    """
                    SELECT attempts, first_attempt_at, blocked_until
                    FROM login_failures_state
                    WHERE identifier_hash = %s
                    """,
                    (key,),
                )
                row = normalize_row(cursor.fetchone())
                if not row:
                    return

                blocked_until = as_utc_datetime(row.get("blocked_until"))
                if blocked_until and blocked_until > now_dt:
                    audit_log("login_rate_limited", None, {"email_hash": key})
                    raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em alguns minutos.")

                first_attempt = as_utc_datetime(row.get("first_attempt_at"))
                if first_attempt and (now_dt - first_attempt).total_seconds() > LOGIN_FAILURE_WINDOW_SECONDS:
                    cursor.execute("DELETE FROM login_failures_state WHERE identifier_hash = %s", (key,))
                    return
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to read login failure state")

    entry = login_failures.get(key)
    if not entry:
        return
    blocked_until = float(entry.get("blocked_until") or 0)
    if blocked_until > now:
        audit_log("login_rate_limited", None, {"email_hash": key})
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em alguns minutos.")
    first_attempt = float(entry.get("first_attempt") or 0)
    if now - first_attempt > LOGIN_FAILURE_WINDOW_SECONDS:
        login_failures.pop(key, None)



def record_login_failure(email: str) -> None:
    key = login_failure_key(email)
    now_dt = datetime.now(UTC)
    now = now_dt.timestamp()
    if storage_available():
        try:
            blocked_until_dt = None
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    """
                    SELECT attempts, first_attempt_at
                    FROM login_failures_state
                    WHERE identifier_hash = %s
                    """,
                    (key,),
                )
                row = normalize_row(cursor.fetchone())
                first_attempt = as_utc_datetime(row.get("first_attempt_at")) if row else None
                if not row or not first_attempt or (now_dt - first_attempt).total_seconds() > LOGIN_FAILURE_WINDOW_SECONDS:
                    attempts = 1
                    first_attempt_at = now_dt
                else:
                    attempts = int(row["attempts"]) + 1
                    first_attempt_at = first_attempt

                if attempts >= LOGIN_MAX_ATTEMPTS:
                    blocked_until_dt = now_dt + timedelta(seconds=LOGIN_FAILURE_WINDOW_SECONDS)

                cursor.execute(
                    """
                    INSERT INTO login_failures_state
                      (identifier_hash, attempts, first_attempt_at, blocked_until)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (identifier_hash)
                    DO UPDATE SET
                      attempts = EXCLUDED.attempts,
                      first_attempt_at = EXCLUDED.first_attempt_at,
                      blocked_until = EXCLUDED.blocked_until
                    """,
                    (key, attempts, first_attempt_at, blocked_until_dt),
                )
            return
        except Exception:
            logger.exception("Failed to persist login failure state")

    entry = login_failures.get(key)
    if not entry or now - float(entry.get("first_attempt") or 0) > LOGIN_FAILURE_WINDOW_SECONDS:
        entry = {"count": 0, "first_attempt": now, "blocked_until": 0}
    entry["count"] = int(entry["count"]) + 1
    if int(entry["count"]) >= LOGIN_MAX_ATTEMPTS:
        entry["blocked_until"] = now + LOGIN_FAILURE_WINDOW_SECONDS
    login_failures[key] = entry



def clear_login_failures(email: str) -> None:
    key = login_failure_key(email)
    login_failures.pop(key, None)
    if not storage_available():
        return
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM login_failures_state WHERE identifier_hash = %s", (key,))
    except Exception:
        logger.exception("Failed to clear login failure state")



def detect_profile_photo_extension(content_type: str | None, content: bytes) -> str:
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    extension = PROFILE_PHOTO_ALLOWED_CONTENT_TYPES.get(normalized_type)
    if not extension:
        raise HTTPException(status_code=400, detail="Envie uma imagem JPG, PNG ou WebP.")

    valid_signature = (
        (extension == "jpg" and content.startswith(b"\xff\xd8\xff"))
        or (extension == "png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension == "webp" and len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP")
    )
    if not valid_signature:
        raise HTTPException(status_code=400, detail="O arquivo enviado não parece ser uma imagem válida.")
    return extension



def delete_profile_photo_file(avatar_url: str | None) -> None:
    # Delegates to the storage layer, which handles both Supabase and local disk
    # references (and ignores external OAuth avatar URLs).
    storage.remove_avatar(avatar_url)

