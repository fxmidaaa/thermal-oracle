"""Интеграционные фикстуры: пересоздают thermal_test, гонят alembic, дают
ASGI-клиент с реальным пулом. Если PostgreSQL недоступен — тесты скипаются
(поднять: docker compose -f infra/docker-compose.yml up -d).
"""
import asyncio
import os
import subprocess
import sys
import uuid as uuidlib
from pathlib import Path

import asyncpg
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
BASE_URL = os.environ.get(
    "THERMAL_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/thermal"
)
TEST_DB = "thermal_test"


def _with_db(url: str, dbname: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture(scope="session")
def migrated_db_url() -> str:
    async def _recreate():
        admin = await asyncpg.connect(_with_db(BASE_URL, "postgres"), timeout=5)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            await admin.execute(f"CREATE DATABASE {TEST_DB}")
        finally:
            await admin.close()

    try:
        asyncio.run(_recreate())
    except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL недоступен ({exc!r}); поднимите infra/docker-compose.yml")

    url = _with_db(BASE_URL, TEST_DB)
    env = {**os.environ, "THERMAL_DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head упал:\n{result.stdout}\n{result.stderr}"
    )
    return url


@pytest.fixture
async def pool(migrated_db_url):
    p = await asyncpg.create_pool(migrated_db_url, min_size=1, max_size=5)
    yield p
    await p.close()


@pytest.fixture
async def client(pool):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()
    app.state.pool = pool  # lifespan в ASGITransport не запускается — пул свой
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def device(pool):
    """Свежие user+device+token на каждый тест — изоляция без TRUNCATE."""
    from app.services.token_service import issue_device_token

    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users (email) VALUES ($1) RETURNING id",
            f"u-{uuidlib.uuid4().hex[:10]}@test.local",
        )
        device_id = await conn.fetchval(
            """INSERT INTO devices (user_id, name, platform)
               VALUES ($1, 'integration-test', 'windows') RETURNING id""",
            user_id,
        )
        token = await issue_device_token(conn, device_id)
    return device_id, token


# --- хелперы user-API (используются test_user_api и test_maintenance_api) ---

PASSWORD = "secret-password-123"


def _email() -> str:
    # не .local/.test: email-validator отвергает special-use домены (RFC 6761)
    return f"u-{uuidlib.uuid4().hex[:10]}@itest.thermaloracle.io"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def register(client, email: str | None = None) -> tuple[str, uuidlib.UUID]:
    response = await client.post(
        "/api/v1/auth/register", json={"email": email or _email(), "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["access_token"], uuidlib.UUID(body["user_id"])


async def pair_device(client, token: str, name: str = "Test Legion") -> tuple[uuidlib.UUID, str]:
    code = (await client.post("/api/v1/devices/pairing-code", headers=_auth(token))).json()
    response = await client.post(
        "/v1/telemetry/pair",
        json={"code": code["code"], "name": name, "platform": "windows"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuidlib.UUID(body["device_id"]), body["device_token"]
