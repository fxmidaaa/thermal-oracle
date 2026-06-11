"""Криптопримитивы user-auth: bcrypt-пароли и JWT.

bcrypt — CPU-bound по дизайну (~200 мс): вызывающий код в async-обработчиках
обязан уводить hash/verify в thread-pool (asyncio.to_thread), иначе встанет
event loop. Лимит bcrypt 72 байта enforced'ится длиной пароля в Pydantic-схеме.
"""
import datetime as dt
import functools
import uuid

import bcrypt
import jwt

ALGORITHM = "HS256"


def hash_password(password: str, rounds: int = 12) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


@functools.lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Для выравнивания тайминга, когда email не найден (анти-enumeration)."""
    return hash_password("not-a-real-password")


def create_access_token(user_id: uuid.UUID, secret: str, ttl_hours: float) -> str:
    now = dt.datetime.now(dt.UTC)
    payload = {"sub": str(user_id), "iat": now, "exp": now + dt.timedelta(hours=ttl_hours)}
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str, secret: str) -> uuid.UUID | None:
    """→ user_id либо None (просрочен/подделан/мусор) — без исключений."""
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        return uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None
