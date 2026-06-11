"""Сквозные тесты ingest: HTTP → валидация → дедуп → telemetry_raw."""
import datetime as dt
import gzip
import json
import uuid

NOW = lambda: dt.datetime.now(dt.UTC)  # noqa: E731


def make_batch(n=30, *, start=None, batch_id=None, step_s=1, **sample_overrides) -> dict:
    start = start or (NOW() - dt.timedelta(seconds=n))
    samples = []
    for i in range(n):
        samples.append({
            "ts": (start + dt.timedelta(seconds=i * step_s)).isoformat(),
            "cpu_temp": 85.0, "gpu_temp": 70.0,
            "cpu_power": 60.0, "gpu_power": 20.0,
            "fan_rpm": 4000, "process": "stress.exe",
            **sample_overrides,
        })
    return {
        "schema_version": 1,
        "batch_id": str(batch_id or uuid.uuid4()),
        "sent_at": NOW().isoformat(),
        "agent_version": "test-0.1",
        "samples": samples,
    }


async def post(client, token, payload, **kwargs):
    return await client.post(
        "/v1/telemetry", json=payload,
        headers={"Authorization": f"Bearer {token}"}, **kwargs,
    )


async def count_rows(pool, device_id) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM telemetry_raw WHERE device_id = $1", device_id
        )


async def test_happy_path(client, pool, device):
    device_id, token = device
    r = await post(client, token, make_batch(30))
    assert r.status_code == 200
    assert r.json() == {"accepted": 30, "duplicates": 0, "rejected": 0, "status": "ok"}
    assert await count_rows(pool, device_id) == 30


async def test_batch_retry_is_idempotent(client, pool, device):
    """Слой 1: повтор того же batch_id не трогает данные."""
    device_id, token = device
    payload = make_batch(30)
    assert (await post(client, token, payload)).json()["accepted"] == 30
    r = await post(client, token, payload)
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert await count_rows(pool, device_id) == 30


async def test_overlapping_batches_dedup_rows(client, pool, device):
    """Слой 2: реплей спула с новым batch_id, но пересекающимися ts."""
    device_id, token = device
    start = NOW() - dt.timedelta(seconds=60)
    await post(client, token, make_batch(30, start=start))
    # 10 последних старых ts + 5 новых
    overlap = make_batch(15, start=start + dt.timedelta(seconds=20))
    r = await post(client, token, overlap)
    assert r.json() == {"accepted": 5, "duplicates": 10, "rejected": 0, "status": "ok"}
    assert await count_rows(pool, device_id) == 35


async def test_duplicate_ts_within_batch(client, pool, device):
    """Слой 2 покрывает и дубли внутри одного батча (баг агента)."""
    device_id, token = device
    payload = make_batch(2, step_s=0)  # оба сэмпла с одинаковым ts
    r = await post(client, token, payload)
    assert r.json()["accepted"] == 1
    assert r.json()["duplicates"] == 1
    assert await count_rows(pool, device_id) == 1


async def test_out_of_range_value_nulled_with_flag(client, pool, device):
    device_id, token = device
    r = await post(client, token, make_batch(1, cpu_temp=500.0))
    assert r.json()["accepted"] == 1
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cpu_temp, cpu_power, flags FROM telemetry_raw WHERE device_id = $1",
            device_id,
        )
    assert row["cpu_temp"] is None
    assert row["cpu_power"] == 60.0
    assert row["flags"] == 1


async def test_future_samples_rejected(client, pool, device):
    """Слой 3: сломанные часы агента не загрязняют ряд."""
    device_id, token = device
    r = await post(client, token, make_batch(5, start=NOW() + dt.timedelta(hours=1)))
    assert r.json() == {"accepted": 0, "duplicates": 0, "rejected": 5, "status": "ok"}
    assert await count_rows(pool, device_id) == 0


async def test_late_spool_drain_enqueues_reprocess(client, pool, device):
    """Опоздавшие (но в горизонте) данные принимаются и попадают в reprocess_queue."""
    device_id, token = device
    r = await post(client, token, make_batch(10, start=NOW() - dt.timedelta(days=2)))
    assert r.json()["accepted"] == 10
    async with pool.acquire() as conn:
        pending = await conn.fetchval(
            "SELECT count(*) FROM reprocess_queue WHERE device_id = $1 AND processed_at IS NULL",
            device_id,
        )
    assert pending == 1


async def test_gzip_body(client, pool, device):
    device_id, token = device
    body = gzip.compress(json.dumps(make_batch(10)).encode())
    r = await client.post(
        "/v1/telemetry", content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        },
    )
    assert r.status_code == 200
    assert r.json()["accepted"] == 10


async def test_invalid_token_401(client, device):
    _, _token = device
    r = await post(client, "to_invalid_token_aaaaaaaaaaaaaaaa", make_batch(1))
    assert r.status_code == 401


async def test_last_seen_updated(client, pool, device):
    device_id, token = device
    await post(client, token, make_batch(1))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_seen_at, agent_version FROM devices WHERE id = $1", device_id
        )
    assert row["last_seen_at"] is not None
    assert row["agent_version"] == "test-0.1"
