from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import HTTPException

from app.core.config import settings
from app.core.signing import resolve_jwt_secret

PASSWORD_BCRYPT_ROUNDS = 12
PIN_BCRYPT_ROUNDS = 10

# Used so login performs a bcrypt verification even when the email does not exist
# (constant-time-ish defense against user enumeration by response timing).
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"DummyPassword1", bcrypt.gensalt(rounds=PASSWORD_BCRYPT_ROUNDS)).decode("utf-8")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="A senha deve ter pelo menos 8 caracteres.")
    if len(password) > 72:
        raise HTTPException(status_code=400, detail="A senha excede o tamanho permitido.")
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=400, detail="A senha não pode exceder 72 bytes.")
    if not any(char.isdigit() for char in password):
        raise HTTPException(status_code=400, detail="A senha deve conter pelo menos 1 número.")
    if not any(char.isupper() for char in password):
        raise HTTPException(status_code=400, detail="A senha deve conter pelo menos 1 letra maiúscula.")


def _encode_bcrypt_input(value: str) -> bytes:
    # bcrypt silently truncates input past 72 bytes instead of raising, which
    # would make two different passwords sharing a 72-byte prefix hash (and
    # verify) identically. validate_password_strength() already rejects
    # anything over 72 bytes before it reaches here, but this is the load-
    # bearing check — passlib used to raise for us (bcrypt__truncate_error).
    encoded = value.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("bcrypt input exceeds 72 bytes")
    return encoded


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(_encode_bcrypt_input(password), bcrypt.gensalt(rounds=PASSWORD_BCRYPT_ROUNDS))
    return hashed.decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(_encode_bcrypt_input(password), hashed_password.encode("utf-8"))


def validate_pin(pin: str) -> str:
    cleaned = pin.strip()
    if not cleaned.isdigit() or not 4 <= len(cleaned) <= 6:
        raise HTTPException(status_code=400, detail="PIN deve conter de 4 a 6 dígitos numéricos.")
    return cleaned


def hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt(rounds=PIN_BCRYPT_ROUNDS)).decode("utf-8")


def verify_pin(pin: str, pin_hash: str) -> bool:
    return bcrypt.checkpw(pin.encode("utf-8"), pin_hash.encode("utf-8"))


def create_access_token(user_id: str) -> str:
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(hours=settings.access_token_expire_hours)
    payload = {"sub": str(user_id), "iat": int(issued_at.timestamp()), "exp": int(expires_at.timestamp())}
    return jwt.encode(payload, resolve_jwt_secret(), algorithm=settings.jwt_algorithm)
