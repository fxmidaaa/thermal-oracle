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
        saved, _ = await detect_windows_for_device(
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
        saved_again, _ = await detect_windows_for_device(
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


async def test_reprocess_job_consumes_queue(pool, device_record):
    """Сквозной reprocess: очередь → пересчёт ambient+окон → processed_at.
    Регрессия на путаницу id строки очереди с device_id (ловилось только живьём).
    Стухшие окна внутри диапазона (сдвиг границы на полных данных) — выкинуты."""
    from app.analytics.jobs import reprocess_job

    device_id = device_record["id"]
    day = (dt.datetime.now(UTC) - dt.timedelta(days=2)).date()
    t0 = dt.datetime.combine(day, dt.time(3, 0), tzinfo=UTC)
    async with pool.acquire() as conn:
        t_load = await insert_series(conn, device_id, t0, [(40 * 60, 3.2, 30.5)])
        t_end = await insert_series(conn, device_id, t_load, [(120, 60.0, 85.0)])
        # артефакт инкрементального прогона: окно с границей, которой на полных
        # данных не существует, — пересечётся с честным и обязано исчезнуть
        await conn.execute(
            """INSERT INTO rth_windows
                   (device_id, domain, window_start, window_end, duration_s,
                    p_tail, p_cv, t_tail, dtdt_tail, t_ambient,
                    ambient_confidence, rth, stratum, quality, model_version)
               VALUES ($1, 'cpu', $2::timestamptz,
                       $2::timestamptz + interval '40 seconds', 40,
                       60.0, 0.05, 85.0, 0.01, 30.5, 0.9, 0.91, 'p50_80', 0.7, 1)""",
            device_id, t_load + dt.timedelta(seconds=30),
        )
        await conn.execute(
            """INSERT INTO reprocess_queue (device_id, range_start, range_end)
               VALUES ($1, $2, $3)""",
            device_id, t0, t_end,
        )

    await reprocess_job(pool, PARAMS)

    async with pool.acquire() as conn:
        pending = await conn.fetchval(
            "SELECT count(*) FROM reprocess_queue WHERE device_id = $1 "
            "AND processed_at IS NULL", device_id)
        windows = await conn.fetch(
            "SELECT window_start FROM rth_windows WHERE device_id = $1", device_id)
        ambient = await conn.fetchval(
            "SELECT count(*) FROM ambient_estimates WHERE device_id = $1 AND day = $2",
            device_id, day)
    assert pending == 0          # очередь разобрана
    assert len(windows) == 1     # ровно одно окно: стухший дубль не выжил
    assert windows[0]["window_start"] == t_load   # честная граница, не +30с
    assert ambient == 1          # ambient затронутого дня пересчитан


async def test_windows_without_ambient_are_skipped(pool, device_record):
    """Нет ни одной ambient-оценки → окна не пишутся (появятся при reprocess)."""
    device_id = device_record["id"]
    t0 = dt.datetime.now(UTC) - dt.timedelta(hours=2)
    async with pool.acquire() as conn:
        await insert_series(conn, device_id, t0, [(120, 60.0, 85.0)])
        saved, _ = await detect_windows_for_device(
            conn, device_record, t0, t0 + dt.timedelta(seconds=200), PARAMS
        )
        assert saved == 0


async def test_open_window_matures_with_true_boundaries(pool, device_record):
    """Сессия пересекает границу прогона: первый проход её не эмитит (открыта
    на правом крае) и возвращает начало рана; второй — перечитка от него —
    эмитит ровно одно окно с истинным началом. Регрессия живых артефактов:
    фантомы от CV на огрызке и дубли с другим window_start."""
    device_id = device_record["id"]
    day = (dt.datetime.now(UTC) - dt.timedelta(days=1)).date()
    t0 = dt.datetime.combine(day, dt.time(9, 0), tzinfo=UTC)
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ambient_estimates
                   (device_id, day, t_ambient, confidence, idle_minutes,
                    episodes_n, method, model_version)
               VALUES ($1, $2, 25.0, 0.9, 60, 2, 'test', 1)""",
            device_id, day,
        )
        t_load = await insert_series(conn, device_id, t0, [(60, 3.0, 45.0)])
        t_end = await insert_series(conn, device_id, t_load, [(120, 60.0, 85.0)])

        # прогон 1: until режет сессию посередине → окна нет, ран открыт
        saved1, open1 = await detect_windows_for_device(
            conn, device_record, t0, t_load + dt.timedelta(seconds=60), PARAMS)
        assert saved1 == 0
        open_dt = dt.datetime.fromtimestamp(open1["cpu"], dt.UTC)
        assert abs((open_dt - t_load).total_seconds()) <= 2

        # прогон 2 (как сделает джоб): перечитка от начала открытой сессии
        saved2, open2 = await detect_windows_for_device(
            conn, device_record, open_dt, t_end + dt.timedelta(seconds=60), PARAMS)
        assert saved2 == 1
        assert open2 == {}
        windows = await conn.fetch(
            "SELECT window_start FROM rth_windows WHERE device_id = $1", device_id)
    assert len(windows) == 1                      # ни фантома, ни дубля
    assert windows[0]["window_start"] == t_load   # истинное начало сессии


async def test_detect_windows_job_persists_state(pool, device_record):
    """Джоб-уровень: вотермарк и open_since пишутся в analytics_state;
    сессия, закрывшаяся до until (тишина > gap_split), эмитится сразу."""
    from app.analytics.jobs import detect_windows_job

    device_id = device_record["id"]
    now = dt.datetime.now(dt.UTC)
    async with pool.acquire() as conn:
        for d in (0, 1):
            await conn.execute(
                """INSERT INTO ambient_estimates
                       (device_id, day, t_ambient, confidence, idle_minutes,
                        episodes_n, method, model_version)
                   VALUES ($1, current_date - $2::int, 25.0, 0.9, 60, 2, 'test', 1)
                   ON CONFLICT (device_id, day) DO NOTHING""",
                device_id, d)
        t0 = now - dt.timedelta(seconds=500)
        t_load = await insert_series(conn, device_id, t0, [(60, 3.0, 45.0)])
        await insert_series(conn, device_id, t_load, [(120, 60.0, 85.0)])
        await conn.execute(   # _active_devices смотрит на last_seen_at
            "UPDATE devices SET last_seen_at = now() WHERE id = $1", device_id)

    await detect_windows_job(pool, PARAMS)

    async with pool.acquire() as conn:
        state = await conn.fetchrow(
            """SELECT windows_processed_until, open_since FROM analytics_state
               WHERE device_id = $1""", device_id)
        n = await conn.fetchval(
            "SELECT count(*) FROM rth_windows WHERE device_id = $1", device_id)
    assert state is not None
    assert state["windows_processed_until"] is not None
    assert state["open_since"] == "{}"            # asyncpg отдаёт jsonb строкой
    assert n == 1


async def _insert_rth_windows(conn, device_id, rows):
    """rows: [(window_start, rth, p_tail, fan_rpm)] — прямая вставка точек
    (тренд читает rth_windows, raw для трендовых сценариев не нужен)."""
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


async def test_current_level_respects_epoch_boundary(pool, device_record):
    """Repaste позавчера: окна ПРОШЛОЙ эпохи ещё в «последних 7 днях», но
    rth_current обязан считаться только по новой эпохе — иначе сразу после
    замены пасты health сравнивает базлайн со смесью двух паст (§5.5)."""
    device_id = device_record["id"]
    now = dt.datetime.now(dt.UTC)
    repaste_at = now - dt.timedelta(hours=36)
    async with pool.acquire() as conn:
        old = [(now - dt.timedelta(days=back, hours=2 * k), 1.0, 60.0, 3500.0)
               for back in range(2, 7) for k in range(5)]
        new = [(now - dt.timedelta(hours=3 * k + 1), 0.7, 60.0, 3500.0)
               for k in range(6)]
        await _insert_rth_windows(conn, device_id, old + new)
        await conn.execute(
            """INSERT INTO maintenance_events (device_id, ts, kind, tim_type)
               VALUES ($1, $2, 'repaste', 'ptm7950')""",
            device_id, repaste_at,
        )

        await update_trends_for_device(conn, device_record, PARAMS)
        health = await conn.fetchrow(
            "SELECT * FROM device_health WHERE device_id = $1 AND domain = 'cpu'", device_id
        )
    assert health["epoch_start"] == repaste_at
    # медиана смеси (25×1.0 + 6×0.7) дала бы 1.0 — утечка прошлой эпохи
    assert health["rth_current"] == pytest.approx(0.7, abs=0.01)
    assert health["rth_baseline"] is None          # в новой эпохе < 30 окон
    assert health["data_quality"] == "sparse"      # и < 10 дней — честно ждём


async def test_changepoint_emits_suggested_maintenance(pool, device_record):
    """Ступенька дневных медиан (−20%: типичная чистка кулеров) →
    предложение maintenance_events source='changepoint_suggested' (§5.5):
    ровно одно (дедуп при повторном прогоне), эпоху НЕ режет, тренд считается
    по сегменту после ступеньки."""
    device_id = device_record["id"]
    rng = np.random.default_rng(7)
    now = dt.datetime.now(dt.UTC)
    rows = []
    for day_back in range(30, 0, -1):
        level = 1.0 if day_back > 15 else 0.8
        rows.extend(
            (now - dt.timedelta(days=day_back, hours=-2 * k),
             level + float(rng.normal(0, 0.01)), 60.0, 3500.0)
            for k in range(4)
        )
    async with pool.acquire() as conn:
        await _insert_rth_windows(conn, device_id, rows)

        await update_trends_for_device(conn, device_record, PARAMS)
        await update_trends_for_device(conn, device_record, PARAMS)  # идемпотентность

        suggested = await conn.fetch(
            """SELECT ts, kind, source, note FROM maintenance_events
               WHERE device_id = $1 AND source = 'changepoint_suggested'""",
            device_id,
        )
        health = await conn.fetchrow(
            "SELECT * FROM device_health WHERE device_id = $1 AND domain = 'cpu'", device_id
        )

    assert len(suggested) == 1                      # дедуп: не по строке на прогон
    event = suggested[0]
    assert event["kind"] == "regime_change"
    step_day = (now - dt.timedelta(days=15)).date()
    assert abs((event["ts"].date() - step_day).days) <= 2
    assert "вниз" in event["note"] and "cpu" in event["note"]

    # неподтверждённое предложение базлайн не сбрасывает: эпоха = начало данных
    assert health["epoch_start"].date() == (now - dt.timedelta(days=30)).date()
    # тренд — по плоскому сегменту ПОСЛЕ ступеньки: наклон незначим
    assert health["data_quality"] == "ok"
    assert health["slope_mkw_per_30d"] is not None
    assert health["slope_ci_low"] <= 0 <= health["slope_ci_high"]


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
