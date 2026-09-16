"""Avatar storage abstraction.

Two backends, selected at call time:

- **Supabase Storage** (private bucket) when ``SUPABASE_URL`` and
  ``SUPABASE_SERVICE_ROLE_KEY`` are configured. Required on serverless (Vercel)
  where the filesystem is ephemeral/read-only, and it gives real access control
  (objects are served through short-lived signed URLs, never public).
- **Local disk** fallback for development, preserving the previous behavior.

Stored references (persisted in ``users.avatar_url``) are opaque:
- ``supabase://<path>``            → object in the private bucket
- ``/media/profile-photos/<file>`` → local disk file
- ``http(s)://...`` / ``data:...`` → external (e.g. OAuth), passed through
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("trevo.storage")

SUPABASE_REF_PREFIX = "supabase://"
SIGNED_URL_TTL_SECONDS = 60 * 60

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROFILE_PHOTO_DIR = BASE_DIR / "data" / "profile-photos"
PROFILE_PHOTO_URL_PREFIX = "/media/profile-photos"

_client = None


def _supabase_client():
    global _client
    if _client is not None:
        return _client
    from supabase import create_client  # lazy import; heavy and optional

    _client = create_client(
        settings.effective_supabase_url,
        settings.effective_supabase_service_role_key,
    )
    return _client


def _is_external(ref: str) -> bool:
    return ref.startswith(("http://", "https://", "data:"))


def store_avatar(user_id: str, content: bytes, extension: str, content_type: str) -> str:
    """Persist avatar bytes and return an opaque stored reference."""
    if settings.supabase_configured:
        path = f"{user_id}/{secrets.token_hex(8)}.{extension}"
        bucket = settings.effective_avatars_bucket
        _supabase_client().storage.from_(bucket).upload(
            path,
            content,
            {"content-type": content_type, "upsert": "true"},
        )
        return f"{SUPABASE_REF_PREFIX}{path}"

    PROFILE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{user_id}-{secrets.token_hex(8)}.{extension}"
    target = (PROFILE_PHOTO_DIR / filename).resolve()
    if target.parent != PROFILE_PHOTO_DIR.resolve():
        raise ValueError("Invalid avatar filename.")
    target.write_bytes(content)
    return f"{PROFILE_PHOTO_URL_PREFIX}/{filename}"


def resolve_avatar_url(ref: str | None) -> str | None:
    """Turn a stored reference into a browser-usable URL.

    External URLs and local disk paths pass through unchanged; Supabase refs are
    signed on demand with a short TTL so the bucket can stay private.
    """
    if not ref:
        return None
    if _is_external(ref) or ref.startswith(PROFILE_PHOTO_URL_PREFIX):
        return ref
    if ref.startswith(SUPABASE_REF_PREFIX):
        path = ref[len(SUPABASE_REF_PREFIX):]
        try:
            bucket = settings.effective_avatars_bucket
            result = _supabase_client().storage.from_(bucket).create_signed_url(path, SIGNED_URL_TTL_SECONDS)
            return result.get("signedURL") or result.get("signedUrl") or result.get("signed_url")
        except Exception:
            logger.exception("Failed to sign avatar URL")
            return None
    return ref


def remove_avatar(ref: str | None) -> bool:
    """Best-effort removal of a stored avatar (used on replace and on erasure).

    Returns True on success (or when there's nothing to remove), False when
    removal was attempted and failed. SEC-11: the caller — not this function
    — decides what to do with a failure; on account deletion, it means the
    LGPD erasure is incomplete and needs to be tracked, not silently dropped.
    """
    if not ref or _is_external(ref):
        return True
    try:
        if ref.startswith(SUPABASE_REF_PREFIX):
            path = ref[len(SUPABASE_REF_PREFIX):]
            bucket = settings.effective_avatars_bucket
            _supabase_client().storage.from_(bucket).remove([path])
            return True
        if ref.startswith(PROFILE_PHOTO_URL_PREFIX):
            filename = ref.rsplit("/", 1)[-1]
            target = (PROFILE_PHOTO_DIR / filename).resolve()
            if target.parent == PROFILE_PHOTO_DIR.resolve() and target.exists():
                target.unlink()
        return True
    except Exception:
        logger.exception("Failed to remove avatar")
        return False
