"""Maintenance API: журнал обслуживания режет эпохи и сбрасывает базлайн."""
import datetime as dt

from tests.integration.conftest import _auth, pair_device, register

NOW = lambda: dt.datetime.now(dt.UTC)  # noqa: E731


async def seed_degrading_windows(pool, device_id, days=30, per_day=4):
    """Месяц «деградирующей» истории Rth: хватает и на базлайн (≥30 окон в
    первые 14 дней), и на публикацию тренда (≥10 дней, ≥100 окон)."""
    async with pool.acquire() as conn:
        rows = []
        for day_back in range(days, 0, -1):
            day_start = NOW() - dt.timedelta(days=day_back)
            rth = 1.0 + 0.004 * (days - day_back)
            rows.extend(
                (device_id, day_start + dt.timedelta(hours=2 * k), rth + 0.002 * k)
                for k in range(per_day)
            )
        await conn.executemany(
            """INSERT INTO rth_windows
                   (device_id, domain, window_start, window_end, duration_s,
                    p_tail, p_cv, t_tail, dtdt_tail, fan_rpm_avg, t_ambient,
                    ambient_confidence, rth, stratum, quality, model_version)
               VALUES ($1, 'cpu', $2::timestamptz,
                       $2::timestamptz + interval '60 seconds', 60,
                       60.0, 0.05, 85.0, 0.01, 4000.0, 25.0, 0.9, $3, 'p50_80', 0.8, 1)
               ON CONFLICT DO NOTHING""",
            rows,
        )


async def post_maintenance(client, token, device_id, **overrides):
    payload = {
        "maintenance_type": "paste_replacement",
        "performed_at": NOW().isoformat(),
        "notes": "PTM7950 после чистки",
        "tim_type": "ptm7950",
        **overrides,
    }
    return await client.post(
        f"/api/v1/devices/{device_id}/maintenance", json=payload, headers=_auth(token)
    )


async def test_maintenance_resets_epoch_and_baseline(client, pool):
    """Главный сценарий: деградация видна → repaste → health сброшен в новую
    эпоху, старые окна не удалены, но в деградацию больше не входят."""
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    await seed_degrading_windows(pool, device_id)

    # «до»-состояние получаем тем же эндпоинтом: чистка за день ДО начала
    # истории → эпоха покрывает все 30 дней данных, базлайн = первые 14 дней
    before = await post_maintenance(
        client, token, device_id,
        maintenance_type="dust_cleaning",
        performed_at=(NOW() - dt.timedelta(days=31)).isoformat(),
    )
    assert before.status_code == 201
    health = (await client.get(
        f"/api/v1/devices/{device_id}/health", headers=_auth(token))).json()
    cpu = next(h for h in health if h["domain"] == "cpu")
    assert cpu["data_quality"] == "ok"
    assert cpu["degradation_pct"] > 5          # деградация месяца видна
    assert cpu["slope_mkw_per_30d"] > 0

    # --- сама замена пасты: новая эпоха от «сейчас» ---
    response = await post_maintenance(client, token, device_id)
    assert response.status_code == 201
    body = response.json()
    assert body["maintenance_type"] == "paste_replacement"
    assert body["tim_type"] == "ptm7950"

    health = (await client.get(
        f"/api/v1/devices/{device_id}/health", headers=_auth(token))).json()
    cpu = next(h for h in health if h["domain"] == "cpu")
    assert cpu["data_quality"] == "sparse"      # в новой эпохе данных ещё нет
    assert cpu["degradation_pct"] is None
    assert cpu["rth_baseline"] is None
    assert cpu["slope_mkw_per_30d"] is None
    epoch = dt.datetime.fromisoformat(cpu["epoch_start"])
    assert abs((epoch - NOW()).total_seconds()) < 60

    async with pool.acquire() as conn:        # журнал — не удаление истории
        kept = await conn.fetchval(
            "SELECT count(*) FROM rth_windows WHERE device_id = $1", device_id)
    assert kept == 120


async def test_maintenance_ownership_404(client):
    token_a, _ = await register(client)
    device_a, _ = await pair_device(client, token_a)
    token_b, _ = await register(client)
    response = await post_maintenance(client, token_b, device_a)
    assert response.status_code == 404


async def test_maintenance_future_rejected(client):
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    response = await post_maintenance(
        client, token, device_id, performed_at=(NOW() + dt.timedelta(hours=1)).isoformat()
    )
    assert response.status_code == 422


async def test_maintenance_journal_listing(client):
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    assert (await post_maintenance(client, token, device_id)).status_code == 201
    assert (await post_maintenance(
        client, token, device_id,
        maintenance_type="repad", tim_type=None, notes=None,
    )).status_code == 201

    body = (await client.get(
        f"/api/v1/devices/{device_id}/maintenance", headers=_auth(token))).json()
    assert [e["maintenance_type"] for e in body] == ["repad", "paste_replacement"]
    assert body[1]["notes"] == "PTM7950 после чистки"


async def test_suggestions_list_and_confirmation_flow(client, pool):
    """Открытое предложение видно в /maintenance-suggestions; подтверждение —
    обычный POST /maintenance в ±3 дня от него — закрывает предложение,
    но журнал хранит обе записи (история не удаляется)."""
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    suggested_ts = NOW() - dt.timedelta(days=5)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO maintenance_events (device_id, ts, kind, source, note)
               VALUES ($1, $2, 'regime_change', 'changepoint_suggested',
                       'CUSUM: ступенька вниз на 18.0% (cpu/p50_80)')""",
            device_id, suggested_ts,
        )

    url = f"/api/v1/devices/{device_id}/maintenance-suggestions"
    body = (await client.get(url, headers=_auth(token))).json()
    assert len(body) == 1
    assert "CUSUM" in body[0]["note"]
    suggested_at = dt.datetime.fromisoformat(body[0]["suggested_at"])
    assert abs((suggested_at - suggested_ts).total_seconds()) < 1

    # кнопка «Подтвердить» на фронте = существующий POST /maintenance
    confirmed = await post_maintenance(
        client, token, device_id,
        maintenance_type="dust_cleaning", tim_type=None,
        performed_at=(suggested_ts + dt.timedelta(hours=12)).isoformat(),
    )
    assert confirmed.status_code == 201

    assert (await client.get(url, headers=_auth(token))).json() == []
    journal = (await client.get(
        f"/api/v1/devices/{device_id}/maintenance", headers=_auth(token))).json()
    assert len(journal) == 2                  # предложение осталось историей


async def test_suggested_regime_change_renders_in_journal(client, pool):
    """CUSUM-предложение (kind вне пользовательского enum) обязано читаться
    из журнала как есть, а POST юзера такой вид завести не может."""
    token, _ = await register(client)
    device_id, _ = await pair_device(client, token)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO maintenance_events (device_id, ts, kind, source, note)
               VALUES ($1, now() - interval '2 days', 'regime_change',
                       'changepoint_suggested', 'CUSUM: ступенька −18%')""",
            device_id,
        )

    body = (await client.get(
        f"/api/v1/devices/{device_id}/maintenance", headers=_auth(token))).json()
    assert body[0]["maintenance_type"] == "regime_change"
    assert body[0]["source"] == "changepoint_suggested"

    rejected = await post_maintenance(
        client, token, device_id, maintenance_type="regime_change")
    assert rejected.status_code == 422
