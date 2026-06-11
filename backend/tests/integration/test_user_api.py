"""User API против реальной БД: JWT-циклы, паринг полного круга
(код → устройство → телеметрия), строгая изоляция тенантов, витрина данных."""
import datetime as dt
import uuid

import pytest

from app.security import create_access_token
from app.settings import Settings
from tests.integration.conftest import PASSWORD, _auth, _email, pair_device, register

# ----------------------------------------------------------------- auth --

async def test_register_then_login(client):
    email = _email()
    token, user_id = await register(client, email)
    assert token

    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200
    assert uuid.UUID(response.json()["user_id"]) == user_id


async def test_duplicate_email_409(client):
    email = _email()
    await register(client, email)
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 409


async def test_login_wrong_password_401(client):
    email = _email()
    await register(client, email)
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert response.status_code == 401


async def test_login_unknown_email_401(client):
    response = await client.post(
        "/api/v1/auth/login", json={"email": _email(), "password": PASSWORD}
    )
    assert response.status_code == 401


async def test_garbage_jwt_401(client):
    response = await client.get("/api/v1/devices", headers=_auth("not.a.jwt"))
    assert response.status_code == 401


async def test_expired_jwt_401(client):
    token, _user_id = await register(client)
    del token
    expired = create_access_token(uuid.uuid4(), Settings().jwt_secret, ttl_hours=-1)
    response = await client.get("/api/v1/devices", headers=_auth(expired))
    assert response.status_code == 401


async def test_no_auth_header_401(client):
    assert (await client.get("/api/v1/devices")).status_code == 401


# -------------------------------------------------------------- pairing --

async def test_pairing_full_circle(client, pool):
    """Код из дашборда → агент пейрится → устройство в профиле →
    полученный device token реально принимает телеметрию."""
    token, user_id = await register(client)
    device_id, device_token = await pair_device(client, token, name="Full Circle PC")

    devices = (await client.get("/api/v1/devices", headers=_auth(token))).json()
    assert [d["name"] for d in devices] == ["Full Circle PC"]
    assert uuid.UUID(devices[0]["id"]) == device_id

    async with pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT user_id FROM devices WHERE id = $1", device_id)
        assert owner == user_id

    now = dt.datetime.now(dt.UTC)
    batch = {
        "schema_version": 1,
        "batch_id": str(uuid.uuid4()),
        "sent_at": now.isoformat(),
        "agent_version": "pair-test",
        "samples": [
            {"ts": (now - dt.timedelta(seconds=i)).isoformat(),
             "cpu_temp": 60.0, "cpu_power": 20.0}
            for i in range(5)
        ],
    }
    response = await client.post(
        "/v1/telemetry", json=batch, headers=_auth(device_token))
    assert response.status_code == 200
    assert response.json()["accepted"] == 5


async def test_pairing_code_is_single_use(client):
    token, _ = await register(client)
    code = (await client.post(
        "/api/v1/devices/pairing-code", headers=_auth(token))).json()["code"]
    body = {"code": code, "name": "First", "platform": "windows"}
    assert (await client.post("/v1/telemetry/pair", json=body)).status_code == 201
    second = await client.post("/v1/telemetry/pair", json={**body, "name": "Second"})
    assert second.status_code == 400


async def test_pairing_invalid_code_400(client):
    response = await client.post(
        "/v1/telemetry/pair",
        json={"code": "ZZZZ-9999", "name": "x", "platform": "windows"},
    )
    assert response.status_code == 400


async def test_pairing_expired_code_400(client, pool):
    from app.services.pairing_service import create_pairing_code

    _token, user_id = await register(client)
    async with pool.acquire() as conn:
        code, _ = await create_pairing_code(conn, user_id, ttl_minutes=-1)
    response = await client.post(
        "/v1/telemetry/pair", json={"code": code, "name": "x", "platform": "windows"}
    )
    assert response.status_code == 400


async def test_pairing_code_normalization(client):
    """Код, введённый строчными и без дефиса, всё равно срабатывает."""
    token, _ = await register(client)
    code = (await client.post(
        "/api/v1/devices/pairing-code", headers=_auth(token))).json()["code"]
    mangled = code.replace("-", "").lower()
    response = await client.post(
        "/v1/telemetry/pair", json={"code": mangled, "name": "x", "platform": "windows"}
    )
    assert response.status_code == 201


# ------------------------------------------------------------ isolation --

async def test_tenant_isolation(client):
    """User B не видит устройство User A ни в списке, ни по прямым URL."""
    token_a, _ = await register(client)
    device_a, _ = await pair_device(client, token_a, name="A's laptop")

    token_b, _ = await register(client)
    assert (await client.get("/api/v1/devices", headers=_auth(token_b))).json() == []

    for path in (f"/api/v1/devices/{device_a}/health",
                 f"/api/v1/devices/{device_a}/current-health",
                 f"/api/v1/devices/{device_a}/rth-history",
                 f"/api/v1/devices/{device_a}/maintenance-suggestions",
                 f"/api/v1/devices/{device_a}/trend",
                 f"/api/v1/devices/{device_a}/timeseries"):
        response = await client.get(path, headers=_auth(token_b))
        assert response.status_code == 404, path     # не 403: существование не раскрываем

    # владелец при этом всё видит
    assert (await client.get(
        f"/api/v1/devices/{device_a}/health", headers=_auth(token_a))).status_code == 200


# ---------------------------------------------------------------- data --

async def test_health_endpoint(client, pool):
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO device_health
                   (device_id, domain, computed_at, rth_baseline, rth_current,
                    degradation_pct, forecast_throttle_date, health_score,
                    data_quality, diagnosis, model_version)
               VALUES ($1, 'cpu', now(), 1.0, 1.12, 12.0, current_date + 90, 61,
                       'ok', 'tim_degradation', 1)""",
            device_id,
        )
    body = (await client.get(
        f"/api/v1/devices/{device_id}/health", headers=_auth(token))).json()
    assert len(body) == 1
    assert body[0]["domain"] == "cpu"
    assert body[0]["diagnosis"] == "tim_degradation"
    assert body[0]["health_score"] == 61
    assert body[0]["degradation_pct"] == 12.0
    assert body[0]["days_to_critical"] == 90       # вычисляется из forecast-даты


async def _insert_window(conn, device_id, *, domain="cpu", days_back=0.0,
                         rth=1.0, quality=0.8, stratum="p50_80"):
    await conn.execute(
        """INSERT INTO rth_windows
               (device_id, domain, window_start, window_end, duration_s,
                p_tail, p_cv, t_tail, dtdt_tail, t_ambient,
                ambient_confidence, rth, stratum, quality, model_version)
           VALUES ($1, $2, $3::timestamptz,
                   $3::timestamptz + interval '60 seconds', 60,
                   60.0, 0.05, 85.0, 0.01, 25.0, 0.9, $4, $5, $6, 1)""",
        device_id, domain,
        dt.datetime.now(dt.UTC) - dt.timedelta(days=days_back),
        rth, stratum, quality,
    )


async def test_current_health_endpoint(client, pool):
    """Агрегат шапки: ambient + последняя ЧЕСТНАЯ точка (свежее окно ниже
    гейта не подменяет её) + sparse-снапшот с нуллами вместо цифр."""
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ambient_estimates
                   (device_id, day, t_ambient, confidence, idle_minutes,
                    episodes_n, method, model_version)
               VALUES ($1, current_date, 47.0, 0.17, 31, 4, 'idle_p10_v1', 1)""",
            device_id,
        )
        await conn.execute(
            """INSERT INTO device_health
                   (device_id, domain, computed_at, rth_current, data_quality,
                    diagnosis, model_version)
               VALUES ($1, 'cpu', now(), 0.33, 'sparse', 'insufficient_data', 1)""",
            device_id,
        )
        await _insert_window(conn, device_id, days_back=0.10, rth=0.45, quality=0.8)
        await _insert_window(conn, device_id, days_back=0.05, rth=9.90, quality=0.30)

    body = (await client.get(
        f"/api/v1/devices/{device_id}/current-health", headers=_auth(token))).json()
    assert body["t_ambient"] == 47.0
    assert abs(body["ambient_confidence"] - 0.17) < 0.005
    assert len(body["domains"]) == 1                   # gpu без данных не приходит
    cpu = body["domains"][0]
    assert cpu["domain"] == "cpu"
    assert cpu["rth_latest"] == pytest.approx(0.45)    # мусорная точка не пролезла
    assert cpu["rth_current"] == pytest.approx(0.33)
    assert cpu["health_score"] is None                 # sparse — честный null
    assert cpu["data_quality"] == "sparse"
    assert cpu["days_to_critical"] is None


async def test_current_health_empty_device(client):
    """Свежеспаренное устройство: 200 с пустыми полями, не 404/500."""
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    body = (await client.get(
        f"/api/v1/devices/{device_id}/current-health", headers=_auth(token))).json()
    assert body["t_ambient"] is None
    assert body["domains"] == []


async def test_rth_history_respects_gate_window_and_overrides(client, pool):
    """Скаттер: только домен cpu, только окна ≥ per-device гейта, только
    период days; ASC; поднятый через overrides гейт сужает выборку."""
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    async with pool.acquire() as conn:
        await _insert_window(conn, device_id, days_back=2, rth=0.40, quality=0.55)
        await _insert_window(conn, device_id, days_back=1, rth=0.42, quality=0.80)
        await _insert_window(conn, device_id, days_back=40, rth=0.38, quality=0.80)
        await _insert_window(conn, device_id, days_back=1, rth=0.10, quality=0.30)
        await _insert_window(conn, device_id, days_back=1, rth=0.50, quality=0.80,
                             domain="gpu")

    url = f"/api/v1/devices/{device_id}/rth-history"
    body = (await client.get(url, headers=_auth(token))).json()
    assert body["domain"] == "cpu" and body["days"] == 30
    assert body["quality_gate"] == 0.5
    # ASC, без мусора и чужих доменов
    assert [p["rth"] for p in body["points"]] == pytest.approx([0.40, 0.42])

    body = (await client.get(url + "?days=90", headers=_auth(token))).json()
    assert len(body["points"]) == 3                    # старая точка вернулась

    async with pool.acquire() as conn:                 # фронт видит гейт устройства
        await conn.execute(
            """UPDATE devices SET analysis_overrides = '{"quality_min": 0.7}'::jsonb
               WHERE id = $1""", device_id)
    body = (await client.get(url, headers=_auth(token))).json()
    assert body["quality_gate"] == 0.7
    assert [p["rth"] for p in body["points"]] == pytest.approx([0.42])


async def test_trend_endpoint_with_auto_stratum(client, pool):
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    now = dt.datetime.now(dt.UTC)
    async with pool.acquire() as conn:
        for day_back in range(3):
            for k in range(4):
                await conn.execute(
                    """INSERT INTO rth_windows
                           (device_id, domain, window_start, window_end, duration_s,
                            p_tail, p_cv, t_tail, dtdt_tail, t_ambient,
                            ambient_confidence, rth, stratum, quality, model_version)
                       VALUES ($1, 'cpu', $2::timestamptz,
                               $2::timestamptz + interval '60 seconds', 60,
                               60.0, 0.05, 85.0, 0.01, 25.0, 0.9, $3, 'p50_80', 0.8, 1)""",
                    device_id,
                    now - dt.timedelta(days=day_back, hours=k),
                    1.0 + 0.01 * day_back + 0.005 * k,
                )
    body = (await client.get(
        f"/api/v1/devices/{device_id}/trend", headers=_auth(token))).json()
    assert body["domain"] == "cpu"
    assert body["stratum"] == "p50_80"               # headline подобралась сама
    assert len(body["points"]) == 3
    point = body["points"][-1]
    assert point["windows_n"] == 4
    assert point["rth_p25"] <= point["rth_median"] <= point["rth_p75"]


async def test_trend_endpoint_no_data_404(client):
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    response = await client.get(
        f"/api/v1/devices/{device_id}/trend", headers=_auth(token))
    assert response.status_code == 404


async def test_timeseries_from_realtime_cagg(client, pool):
    """CAgg в real-time режиме отдаёт свежие данные без refresh'а."""
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    # выравниваем по границе минуты, иначе первый бакет — частичный
    start = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30)).replace(
        second=0, microsecond=0)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO telemetry_raw (device_id, ts, cpu_temp, cpu_power, fan_rpm)
               SELECT $1, * FROM unnest($2::timestamptz[], $3::real[], $4::real[], $5::int[])""",
            device_id,
            [start + dt.timedelta(seconds=i) for i in range(180)],
            [70.0 + (i % 3) for i in range(180)],
            [50.0] * 180,
            [4000] * 180,
        )
    body = (await client.get(
        f"/api/v1/devices/{device_id}/timeseries?bucket=1m", headers=_auth(token))).json()
    assert len(body) >= 3                            # ~3 минутных бакета
    bucket = body[0]
    assert bucket["n"] == 60                         # выровненный старт → полный бакет
    assert 69.5 <= bucket["cpu_temp_avg"] <= 72.5
    assert bucket["cpu_power_max"] == 50.0
    assert bucket["fan_rpm_avg"] == 4000


async def test_timeseries_range_validation(client):
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    response = await client.get(
        f"/api/v1/devices/{device_id}/timeseries?bucket=1m"
        f"&from=2026-01-01T00:00:00Z&to=2026-01-10T00:00:00Z",
        headers=_auth(token),
    )
    assert response.status_code == 422               # 9 дней для 1m — слишком широко
