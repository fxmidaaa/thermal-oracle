"""Токены устройств: выпуск и аутентификация.

Формат: "to_<43 url-safe символа>". В БД хранится только SHA-256; плэйнтекст
показывается ровно один раз при выпуске. Lookup — по token_prefix (индекс),
сравнение хэшей — constant-time. Кэш положительных ответов на 60с снимает
запрос к БД с каждого батча; отзыв токена начинает действовать максимум через
TTL — осознанный трейд-офф (architecture.md §6).
"""
import hashlib
import hmac
import time
import uuid

import asyncpg

TOKEN_PREFIX_LEN = 12
_CACHE_TTL_S = 60.0
_CACHE_MAX = 10_000

# sha256(token) -> (device_id, monotonic-время записи)
_auth_cache: dict[bytes, tuple[uuid.UUID, float]] = {}


def generate_token() -> tuple[str, str, bytes]:
    """→ (плэйнтекст, prefix, sha256). Плэйнтекст наружу, остальное — в БД."""
    import secrets

    raw = "to_" + secrets.token_urlsafe(32)
    return raw, raw[:TOKEN_PREFIX_LEN], hashlib.sha256(raw.encode()).digest()


async def issue_device_token(conn: asyncpg.Connection, device_id: uuid.UUID) -> str:
    raw, prefix, digest = generate_token()
    await conn.execute(
        "INSERT INTO device_tokens (device_id, token_prefix, token_hash) VALUES ($1, $2, $3)",
        device_id, prefix, digest,
    )
    return raw


async def authenticate_device(conn: asyncpg.Connection, raw_token: str) -> uuid.UUID | None:
    if len(raw_token) < TOKEN_PREFIX_LEN:
        return None
    digest = hashlib.sha256(raw_token.encode()).digest()

    cached = _auth_cache.get(digest)
    if cached is not None and time.monotonic() - cached[1] < _CACHE_TTL_S:
        return cached[0]

    rows = await conn.fetch(
        """SELECT device_id, token_hash FROM device_tokens
           WHERE token_prefix = $1 AND revoked_at IS NULL""",
        raw_token[:TOKEN_PREFIX_LEN],
    )
    for row in rows:
        if hmac.compare_digest(bytes(row["token_hash"]), digest):
            if len(_auth_cache) >= _CACHE_MAX:
                _auth_cache.clear()
            _auth_cache[digest] = (row["device_id"], time.monotonic())
            await conn.execute(
                "UPDATE device_tokens SET last_used_at = now() WHERE token_hash = $1",
                digest,
            )
            return row["device_id"]
    return None
