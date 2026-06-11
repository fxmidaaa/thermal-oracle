"""Криптослой: bcrypt и JWT (без БД). rounds=4 — только чтобы тесты летали."""
import uuid

from app.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "test-secret-of-sufficient-hmac-length-1234"  # ≥32 байт — иначе PyJWT ругается
USER = uuid.uuid4()


def test_password_roundtrip():
    h = hash_password("correct horse battery", rounds=4)
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong password", h)


def test_password_hash_is_salted():
    assert hash_password("same", rounds=4) != hash_password("same", rounds=4)


def test_verify_against_missing_or_garbage_hash():
    assert not verify_password("anything", None)
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_jwt_roundtrip():
    token = create_access_token(USER, SECRET, ttl_hours=1)
    assert decode_access_token(token, SECRET) == USER


def test_jwt_expired_rejected():
    token = create_access_token(USER, SECRET, ttl_hours=-1)
    assert decode_access_token(token, SECRET) is None


def test_jwt_wrong_secret_rejected():
    token = create_access_token(USER, SECRET, ttl_hours=1)
    assert decode_access_token(token, "another-secret-of-sufficient-length-5678") is None


def test_jwt_garbage_rejected():
    assert decode_access_token("definitely.not.jwt", SECRET) is None
