from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException

from app.core.security import (
    create_access_token,
    hash_password,
    hash_pin,
    validate_password_strength,
    validate_pin,
    verify_password,
    verify_pin,
)


def test_hash_and_verify_password():
    hashed = hash_password("Senha123")
    assert hashed != "Senha123"
    assert verify_password("Senha123", hashed) is True
    assert verify_password("Errada123", hashed) is False


def test_verify_password_accepts_hash_generated_by_old_passlib_backend():
    # DEP-05: hashes gravados no banco antes da migração de passlib para
    # bcrypt direto (app/core/security.py) continuam no formato bcrypt
    # padrão — gerado uma vez com CryptContext(schemes=["bcrypt"]) e colado
    # aqui como fixture, para provar que a troca de biblioteca não invalida
    # senha de usuário nenhum.
    old_passlib_hash = "$2b$04$xaKHNRIQFEY9M7NbGEewUOs3srMc5T28rUybRRCwxCwIeF5yFdz0W"
    assert verify_password("Senha123", old_passlib_hash) is True
    assert verify_password("SenhaErrada1", old_passlib_hash) is False


def test_verify_pin_accepts_hash_generated_by_old_passlib_backend():
    old_passlib_hash = "$2b$04$U7HkFiied0QJjqBiq1.JBur92mpTLqldwr39vrEewGb0A1meJwwPS"
    assert verify_pin("1234", old_passlib_hash) is True
    assert verify_pin("9999", old_passlib_hash) is False


def test_hash_and_verify_pin():
    hashed = hash_pin("1234")
    assert hashed != "1234"
    assert verify_pin("1234", hashed) is True
    assert verify_pin("4321", hashed) is False


def test_create_access_token_contains_subject(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "unit-secret-key-for-tests-32-chars")
    token = create_access_token("user-id")
    decoded = jwt.decode(token, "unit-secret-key-for-tests-32-chars", algorithms=["HS256"])
    assert decoded["sub"] == "user-id"
    assert isinstance(decoded["iat"], int)


def test_decode_accepts_token_generated_by_old_jose_backend():
    # DEP-06: tokens de sessão emitidos antes da migração de python-jose para
    # PyJWT (app/api/deps.py, app/oauth.py, app/core/security.py) continuam
    # sendo o mesmo formato HS256 padrão — gerado uma vez com jose.jwt.encode
    # e colado aqui como fixture, com exp em 2100 para nunca expirar sozinho.
    old_jose_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJ1c2VyLWlkIiwiaWF0IjoxNzAwMDAwMDAwLCJleHAiOjQxMDI0NDQ4MDB9."
        "kB8FRg-k1qj2IXT7p0h5CtQr9YtQwXilB0JmUrwHnoE"
    )
    decoded = jwt.decode(old_jose_token, "unit-secret-key-for-tests-32-chars", algorithms=["HS256"])
    assert decoded["sub"] == "user-id"
    assert decoded["iat"] == 1700000000


def test_expired_token_raises_jwterror():
    token = jwt.encode(
        {"sub": "user-id", "exp": int((datetime.now(UTC) - timedelta(seconds=1)).timestamp())},
        "unit-secret-key-for-tests-32-chars",
        algorithm="HS256",
    )
    with pytest.raises(jwt.PyJWTError):
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
