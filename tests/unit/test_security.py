from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from jose import JWTError, jwt

from app.core.security import (
    create_access_token,
    hash_password,
    validate_password_strength,
    validate_pin,
    verify_password,
)


def test_hash_and_verify_password():
    hashed = hash_password("Senha123")
    assert hashed != "Senha123"
    assert verify_password("Senha123", hashed) is True
    assert verify_password("Errada123", hashed) is False


def test_create_access_token_contains_subject(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-secret-key-for-tests-32-chars")
    token = create_access_token("user-id")
    decoded = jwt.decode(token, "unit-secret-key-for-tests-32-chars", algorithms=["HS256"])
    assert decoded["sub"] == "user-id"
    assert isinstance(decoded["iat"], int)


def test_expired_token_raises_jwterror():
    token = jwt.encode(
        {"sub": "user-id", "exp": int((datetime.now(UTC) - timedelta(seconds=1)).timestamp())},
        "unit-secret-key-for-tests-32-chars",
        algorithm="HS256",
    )
    with pytest.raises(JWTError):
        jwt.decode(token, "unit-secret-key-for-tests-32-chars", algorithms=["HS256"])


@pytest.mark.parametrize("password", ["Senha123", "OutraSenha1"])
def test_validate_password_strength_accepts_valid(password):
    validate_password_strength(password)


@pytest.mark.parametrize("password", ["abc", "abcdef1", "ABCDEF1"])
def test_validate_password_strength_rejects_weak(password):
    with pytest.raises(HTTPException):
        validate_password_strength(password)


@pytest.mark.parametrize("pin", ["1234", "123456"])
def test_validate_pin_accepts_numeric(pin):
    assert validate_pin(pin) == pin


@pytest.mark.parametrize("pin", ["123", "1234567", "abcd"])
def test_validate_pin_rejects_invalid(pin):
    with pytest.raises(HTTPException):
        validate_pin(pin)
