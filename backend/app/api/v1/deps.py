"""Зависимости user-API: JWT → user_id и проверка владения устройством.

App-level tenancy (architecture.md §4.1): ЛЮБОЙ доступ к данным устройства
проходит через get_owned_device — чужой device_id даёт 404 (не 403: существование
чужих устройств не раскрываем).
"""
import uuid

from fastapi import Depends, HTTPException, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> uuid.UUID:
    if credentials is None:
        raise HTTPException(status_code=401, detail="authentication required")
    user_id = decode_access_token(
        credentials.credentials, request.app.state.settings.jwt_secret
    )
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    async with request.app.state.pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM users WHERE id = $1", user_id)
    if not exists:  # токен валиден, но пользователь удалён
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return user_id


async def get_owned_device(
    request: Request,
    device_id: uuid.UUID = Path(),
    user_id: uuid.UUID = Depends(get_current_user),
) -> uuid.UUID:
    async with request.app.state.pool.acquire() as conn:
        owned = await conn.fetchval(
            "SELECT 1 FROM devices WHERE id = $1 AND user_id = $2", device_id, user_id
        )
    if not owned:
        raise HTTPException(status_code=404, detail="device not found")
    return device_id
