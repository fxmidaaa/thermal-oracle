"""Сквозной прогон аналитики против реальной TimescaleDB:
raw телеметрия → ambient → окна/Rth → device_health.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.analytics.jobs import (
    detect_windows_for_device,
    estimate_ambient_for_device_day,
    update_trends_for_device,
)
from app.analytics.params import AnalysisParams

UTC = ZoneInfo("UTC")
PARAMS = AnalysisParams()


async def insert_series(conn, device_id, start: dt.datetime, segments):
    """segments: [(длительность_с, cpu_power, cpu_temp)] — вставка 1 Гц рядов."""
    ts, power, temp = [], [], []
    t = start
    for duration, p_w, t_c in segments:
        for _ in range(int(duration)):
            ts.append(t)
            power.append(p_w)
            temp.append(t_c)
            t += dt.timedelta(seconds=1)
    await conn.execute(
        """INSERT INTO telemetry_raw (device_id, ts, cpu_power, cpu_temp, fan_rpm)
           SELECT $1, * FROM unnest($2::timestamptz[], $3::real[], $4::real[], $5::int[])
           ON CONFLICT DO NOTHING""",
        device_id, ts, power, temp, [3000] * len(ts),
    )
    return t


@pytest.fixture
async def device_record(pool, device):
    device_id, _token = device
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, timezone, analysis_overrides FROM devices WHERE id = $1", device_id
        )


async def test_full_analytics_pipeline(pool, device_record):
    device_id = device_record["id"]
    day = (dt.datetime.now(UTC) - dt.timedelta(days=1)).date()
    t0 = dt.datetime.combine(day, dt.time(2, 0), tzinfo=UTC)

    async with pool.acquire() as conn:
        # 40 мин глубокого простоя (прокси-ambient 30.5°), затем 5 мин @60Вт/85°
        t_load = await insert_series(conn, device_id, t0, [(40 * 60, 3.2, 30.5)])
        await insert_series(conn, device_id, t_load, [(300, 60.0, 85.0)])

        # --- ambient: эпизод найден, soak-back хвост дал ≈30.5 ---
        assert await estimate_ambient_for_device_day(conn, device_id, day, UTC, PARAMS)
        amb = await conn.fetchrow(
            "SELECT * FROM ambient_estimates WHERE device_id = $1 AND day = $2",
            device_id, day,
        )
        assert abs(amb["t_ambient"] - 30.5) < 0.3
        assert amb["confidence"] > 0.3

        # --- окна: сессия 300с → 1 окно, Rth ≈ (85−30.5)/60 ≈ 0.91 ---
        saved = await detect_windows_for_device(
            conn, device_record, t0, t_load + dt.timedelta(seconds=400), PARAMS
        )
        assert saved == 1
        window = await conn.fetchrow(
            "SELECT * FROM rth_windows WHERE device_id = $1 AND domain = 'cpu'", device_id
        )
        assert window["stratum"] == "p50_80"
        assert abs(window["rth"] - (85.0 - amb["t_ambient"]) / 60.0) < 0.02
        assert window["quality"] > 0.3
        assert window["fan_rpm_avg"] == pytest.approx(3000, abs=1)

        # повторный прогон того же диапазона — идемпотентный upsert, не дубль
        saved_again = await detect_windows_for_device(
            conn, device_record, t0, t_load + dt.timedelta(seconds=400), PARAMS
        )
        assert saved_again == 1
        n_windows = await conn.fetchval(
            "SELECT count(*) FROM rth_windows WHERE device_id = $1", device_id)
        assert n_windows == 1

        # --- тренд: данных на 1 день → честный 'sparse', без наклона ---
        await update_trends_for_device(conn, device_record, PARAMS)
        health = await conn.fetchrow(
            "SELECT * FROM device_health WHERE device_id = $1 AND domain = 'cpu'", device_id
        )
        assert health is not None
        assert health["data_quality"] == "sparse"
        assert health["slope_mkw_per_30d"] is None
        assert health["diagnosis"] == "insufficient_data"
        assert health["rth_current"] == pytest.approx(window["rth"], rel=0.01)
        assert health["rth_baseline"] is None        # < 30 окон в базлайн-периоде


async def test_windows_without_ambient_are_skipped(pool, device_record):
    """Нет ни одной ambient-оценки → окна не пишутся (появятся при reprocess)."""
    device_id = device_record["id"]
    t0 = dt.datetime.now(UTC) - dt.timedelta(hours=2)
    async with pool.acquire() as conn:
        await insert_series(conn, device_id, t0, [(120, 60.0, 85.0)])
        saved = await detect_windows_for_device(
            conn, device_record, t0, t0 + dt.timedelta(seconds=200), PARAMS
        )
        assert saved == 0


async def test_synthetic_degradation_detected_end_to_end(pool, device_record):
    """Полугодовая «деградация» в rth_windows напрямую: тренд значим, диагноз
    TIM, прогноз в горизонте. Проверяет SQL-агрегации + математику вместе."""
    device_id = device_record["id"]
    rng = np.random.default_rng(1)
    now = dt.datetime.now(dt.UTC)
    async with pool.acquire() as conn:
        rows = []
        for day_back in range(80, 0, -1):
            day_start = now - dt.timedelta(days=day_back)
            base_rth = 1.0 + 0.0025 * (80 - day_back)     # +0.2 K/W за 80 дней
            for k in range(3):                             # 3 окна в день
                rows.append((
                    day_start + dt.timedelta(hours=2 * k),
                    base_rth + float(rng.normal(0, 0.015)),
                    60.0, float(rng.uniform(3000, 5000)),
                ))
        await conn.executemany(
            """INSERT INTO rth_windows
                   (device_id, domain, window_start, window_end, duration_s,
                    p_tail, p_cv, t_tail, dtdt_tail, fan_rpm_avg, t_ambient,
                    ambient_confidence, rth, stratum, quality, model_version)
               VALUES ($1, 'cpu', $2::timestamptz,
                       $2::timestamptz + interval '60 seconds', 60,
                       $4, 0.05, 85.0, 0.01, $5, 25.0, 0.9, $3, 'p50_80', 0.8, 1)
               ON CONFLICT DO NOTHING""",
            [(device_id, ts, rth, p, rpm) for ts, rth, p, rpm in rows],
        )
        await conn.execute(
            """INSERT INTO ambient_estimates
                   (device_id, day, t_ambient, confidence, idle_minutes, episodes_n,
                    method, model_version)
               VALUES ($1, current_date - 1, 25.0, 0.9, 60, 2, 'test', 1)
               ON CONFLICT DO NOTHING""",
            device_id,
        )

        await update_trends_for_device(conn, device_record, PARAMS)
        health = await conn.fetchrow(
            "SELECT * FROM device_health WHERE device_id = $1 AND domain = 'cpu'", device_id
        )
        assert health["data_quality"] == "ok"
        # истинный наклон 0.0025 K/W в день = 75 мК/Вт за 30 дней
        assert health["slope_mkw_per_30d"] == pytest.approx(75.0, rel=0.25)
        assert health["slope_ci_low"] > 0                  # значимо
        assert health["degradation_pct"] > 10
        assert health["diagnosis"] == "tim_degradation"
        assert health["forecast_throttle_date"] is not None
        assert health["health_score"] < 60
