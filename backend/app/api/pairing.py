"""Публичный эндпоинт сопряжения агента (architecture.md, Шаг 5).

Агент в режиме первой настройки шлёт сюда pairing-код, полученный
пользователем в дашборде; в ответ — постоянный device token. Аутентификации
нет осознанно: сам код и есть краткоживущий одноразовый секрет (40 бит на
10 минут). Создание устройства и выпуск токена — одна транзакция с
атомарным claim'ом кода: гонка двух pair с одним кодом даёт одного победителя.
"""
from fastapi import APIRouter, HTTPException, Request

from app.schemas.devices import PairRequest, PairResponse
from app.services.pairing_service import bind_device, redeem_pairing_code
from app.services.token_service import issue_device_token

router = APIRouter(tags=["pairing"])


@router.post("/v1/telemetry/pair", response_model=PairResponse, status_code=201)
async def pair_device(body: PairRequest, request: Request) -> PairResponse:
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            user_id = await redeem_pairing_code(conn, body.code)
            if user_id is None:
                # неверный / просроченный / использованный — наружу не различаем
                raise HTTPException(status_code=400, detail="invalid or expired pairing code")
            device_id = await conn.fetchval(
                """INSERT INTO devices (user_id, name, platform, device_class, agent_version)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                user_id, body.name, body.platform, body.device_class, body.agent_version,
            )
            token = await issue_device_token(conn, device_id)
            await bind_device(conn, body.code, device_id)
    return PairResponse(device_id=device_id, device_token=token)
