"""Регистрация и вход. bcrypt уводится в thread-pool — иначе ~200 мс хэша
блокировали бы event loop на каждом логине."""
import asyncio

from fastapi import APIRouter, HTTPException, Request

from app.schemas.users import LoginRequest, RegisterRequest, TokenResponse
from app.security import create_access_token, dummy_hash, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _token(request: Request, user_id) -> TokenResponse:
    settings = request.app.state.settings
    return TokenResponse(
        access_token=create_access_token(user_id, settings.jwt_secret, settings.jwt_ttl_hours),
        user_id=user_id,
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request) -> TokenResponse:
    password_hash = await asyncio.to_thread(hash_password, body.password)
    async with request.app.state.pool.acquire() as conn:
        user_id = await conn.fetchval(
            """INSERT INTO users (email, password_hash) VALUES ($1, $2)
               ON CONFLICT (email) DO NOTHING RETURNING id""",
            body.email, password_hash,
        )
    if user_id is None:
        raise HTTPException(status_code=409, detail="email already registered")
    return _token(request, user_id)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    async with request.app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE email = $1", body.email
        )
    # неизвестный email: сверяем с dummy-хэшем, чтобы тайминг не выдавал
    # существование аккаунта
    stored_hash = row["password_hash"] if row else dummy_hash()
    ok = await asyncio.to_thread(verify_password, body.password, stored_hash)
    if not ok or row is None:
        raise HTTPException(status_code=401, detail="invalid email or password")
    return _token(request, row["id"])
