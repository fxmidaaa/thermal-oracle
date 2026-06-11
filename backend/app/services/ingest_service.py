"""Горячий путь ingest: санитизация, трёхслойная дедупликация, bulk insert.

Слои дедупа (architecture.md §4.3):
  1. PK (device_id, batch_id) в ingest_batches — HTTP-ретраи целого батча.
     Конкурентный повтор блокируется на вставке PK до коммита первой
     транзакции, затем получает конфликт → ветка duplicate. Корректно при
     любой гонке ретраев.
  2. UNIQUE (device_id, ts) в telemetry_raw + ON CONFLICT DO NOTHING —
     пересечение батчей при реплее оффлайн-спула (и дубли ts внутри батча).
  3. Временные ворота: будущее (> +120с — сломанные часы) и старше 7 дней
     (за горизонтом реанализа == compress_after) не принимаются.

Физические диапазоны НЕ отклоняют сэмпл: поле обнуляется + бит во flags —
сырые данные неизменяемы, фильтрация шума живёт в аналитике (§1.1, §5.3).
"""
import datetime as dt
import uuid
from dataclasses import dataclass

import asyncpg
import structlog

from app.schemas.telemetry import TelemetryBatch, TelemetrySample

log = structlog.get_logger(__name__)

FLAG_OUT_OF_RANGE = 1  # бит 0: значение вне физического диапазона, поле обнулено

# (min, max) включительно; вне диапазона — сенсорный глюк (architecture.md §3)
PHYS_RANGES: dict[str, tuple[float, float]] = {
    "cpu_temp": (-20.0, 120.0),
    "gpu_temp": (-20.0, 120.0),
    "cpu_power": (0.0, 400.0),
    "gpu_power": (0.0, 400.0),
    "fan_rpm": (0, 10_000),
}

FUTURE_TOLERANCE = dt.timedelta(seconds=120)
LATE_HORIZON = dt.timedelta(days=7)  # == compress_after('telemetry_raw') в 0002!
# Старше этого — батч содержит дренаж спула: диапазон уходит в reprocess_queue,
# чтобы worker пересчитал окна/ambient затронутых суток (watermark аналитики
# 2 мин + запас: более свежие данные и так попадут в инкрементальный проход).
REPROCESS_THRESHOLD = dt.timedelta(minutes=10)

_FIELDS = ("cpu_temp", "gpu_temp", "cpu_power", "gpu_power", "fan_rpm")

_process_cache: dict[str, int] = {}
_PROCESS_CACHE_MAX = 10_000


@dataclass
class IngestResult:
    accepted: int
    duplicates: int
    rejected: int
    duplicate_batch: bool = False


def sanitize_sample(
    sample: TelemetrySample, now: dt.datetime
) -> tuple[dict | None, int]:
    """→ (значения | None, flags). None — сэмпл отброшен целиком (слой 3)."""
    if sample.ts > now + FUTURE_TOLERANCE or sample.ts <= now - LATE_HORIZON:
        return None, 0
    flags = 0
    values: dict = {}
    for field in _FIELDS:
        v = getattr(sample, field)
        lo, hi = PHYS_RANGES[field]
        if v is not None and not (lo <= v <= hi):
            v = None
            flags |= FLAG_OUT_OF_RANGE
        values[field] = v
    return values, flags


async def _resolve_process_ids(
    conn: asyncpg.Connection, names: set[str]
) -> dict[str, int]:
    result: dict[str, int] = {}
    missing: list[str] = []
    for name in names:
        cached = _process_cache.get(name)
        if cached is not None:
            result[name] = cached
        else:
            missing.append(name)
    if missing:
        # DO UPDATE (no-op) вместо DO NOTHING: иначе RETURNING молчит про
        # уже существующие имена и пришлось бы делать второй SELECT.
        rows = await conn.fetch(
            """INSERT INTO processes (name)
               SELECT unnest($1::text[])
               ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
               RETURNING id, name""",
            missing,
        )
        if len(_process_cache) + len(rows) > _PROCESS_CACHE_MAX:
            _process_cache.clear()
        for row in rows:
            result[row["name"]] = row["id"]
            _process_cache[row["name"]] = row["id"]
    return result


async def ingest_batch(
    conn: asyncpg.Connection, device_id: uuid.UUID, batch: TelemetryBatch
) -> IngestResult:
    now = dt.datetime.now(dt.UTC)

    async with conn.transaction():
        claimed = await conn.fetchrow(
            """INSERT INTO ingest_batches
                   (device_id, batch_id, sent_at, agent_version, sample_count)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (device_id, batch_id) DO NOTHING
               RETURNING batch_id""",
            device_id, batch.batch_id, batch.sent_at,
            batch.agent_version, len(batch.samples),
        )
        if claimed is None:
            return IngestResult(0, 0, 0, duplicate_batch=True)

        clean: list[tuple[dt.datetime, dict, int, str | None]] = []
        rejected = 0
        for sample in batch.samples:
            values, flags = sanitize_sample(sample, now)
            if values is None:
                rejected += 1
                continue
            clean.append((sample.ts, values, flags, sample.process))

        accepted = 0
        if clean:
            pid_by_name = await _resolve_process_ids(
                conn, {p for _, _, _, p in clean if p}
            )
            ts_arr = [c[0] for c in clean]
            col = lambda f: [c[1][f] for c in clean]  # noqa: E731
            inserted = await conn.fetch(
                """INSERT INTO telemetry_raw
                       (device_id, ts, cpu_temp, gpu_temp, cpu_power, gpu_power,
                        fan_rpm, process_id, flags)
                   SELECT $1::uuid, * FROM unnest(
                       $2::timestamptz[], $3::real[], $4::real[], $5::real[],
                       $6::real[], $7::int[], $8::int[], $9::smallint[])
                   ON CONFLICT (device_id, ts) DO NOTHING
                   RETURNING 1""",
                device_id, ts_arr,
                col("cpu_temp"), col("gpu_temp"), col("cpu_power"), col("gpu_power"),
                col("fan_rpm"),
                [pid_by_name.get(c[3]) if c[3] else None for c in clean],
                [c[2] for c in clean],
            )
            accepted = len(inserted)

        duplicates = len(clean) - accepted
        await conn.execute(
            """UPDATE ingest_batches SET dup_count = $3, rejected_count = $4
               WHERE device_id = $1 AND batch_id = $2""",
            device_id, batch.batch_id, duplicates, rejected,
        )

        if accepted and min(ts_arr) < now - REPROCESS_THRESHOLD:
            await conn.execute(
                """INSERT INTO reprocess_queue (device_id, range_start, range_end)
                   VALUES ($1, $2, $3)""",
                device_id, min(ts_arr), max(ts_arr),
            )

        await conn.execute(
            "UPDATE devices SET last_seen_at = now(), agent_version = $2 WHERE id = $1",
            device_id, batch.agent_version,
        )

    if rejected or duplicates:
        log.info(
            "ingest.batch_partial",
            device_id=str(device_id), batch_id=str(batch.batch_id),
            accepted=accepted, duplicates=duplicates, rejected=rejected,
        )
    return IngestResult(accepted, duplicates, rejected)
