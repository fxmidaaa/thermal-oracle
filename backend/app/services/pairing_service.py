"""Pairing-коды: человеческий пайплайн подключения устройств.

Код формата XXXX-XXXX из алфавита без неоднозначных символов (0/O, 1/I/L) —
читается с экрана и диктуется голосом. 8 символов × 31 буква ≈ 40 бит:
за 10-минутный TTL не перебирается. В БД — только SHA-256.

Одноразовость без гонок: claim — это атомарный
UPDATE ... SET used_at = now() WHERE used_at IS NULL ... RETURNING;
два конкурентных pair с одним кодом — ровно один победитель.
"""
import datetime as dt
import hashlib
import secrets
import uuid

import asyncpg

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # без 0/O/1/I/L
CODE_LEN = 8


def generate_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    return code.replace("-", "").replace(" ", "").strip().upper()


def hash_code(code: str) -> bytes:
    return hashlib.sha256(normalize_code(code).encode()).digest()


async def create_pairing_code(
    conn: asyncpg.Connection, user_id: uuid.UUID, ttl_minutes: float
) -> tuple[str, dt.datetime]:
    # заодно прибираем протухшие неиспользованные коды этого пользователя
    await conn.execute(
        """DELETE FROM pairing_codes
           WHERE user_id = $1 AND used_at IS NULL AND expires_at < now()""",
        user_id,
    )
    code = generate_code()
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=ttl_minutes)
    await conn.execute(
        "INSERT INTO pairing_codes (code_hash, user_id, expires_at) VALUES ($1, $2, $3)",
        hash_code(code), user_id, expires_at,
    )
    return code, expires_at


async def redeem_pairing_code(conn: asyncpg.Connection, code: str) -> uuid.UUID | None:
    """Атомарный одноразовый claim → user_id владельца либо None
    (неверный/просроченный/использованный — наружу не различаем)."""
    return await conn.fetchval(
        """UPDATE pairing_codes SET used_at = now()
           WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now()
           RETURNING user_id""",
        hash_code(code),
    )


async def bind_device(conn: asyncpg.Connection, code: str, device_id: uuid.UUID) -> None:
    await conn.execute(
        "UPDATE pairing_codes SET device_id = $2 WHERE code_hash = $1",
        hash_code(code), device_id,
    )
