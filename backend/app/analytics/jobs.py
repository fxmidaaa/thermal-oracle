"""IO-глю аналитики: чтение raw из TimescaleDB → чистая математика → upsert
производных. Все джобы идемпотентны (upsert по натуральным ключам) — повторный
прогон любого диапазона безопасен (architecture.md §4.4, §5.6).
"""
import datetime as dt
import json
import uuid
from zoneinfo import ZoneInfo

import asyncpg
import numpy as np
import structlog

from app.analytics import ambient as ambient_mod
from app.analytics import trend as trend_mod
from app.analytics.params import MODEL_VERSION, AnalysisParams
from app.analytics.rth import attach_rth
from app.analytics.windows import detect_stable_windows

log = structlog.get_logger(__name__)

WATERMARK_S = 120          # хвост свежих данных ещё доезжает (батчи 30с + ретраи)
LOOKBACK_S = 700           # > window_max(600) + грейс: открытое окно дозреет к след. прогону
REPROCESS_PAD_S = 700.0
DOMAINS = (("cpu", "cpu_power", "cpu_temp"), ("gpu", "gpu_power", "gpu_temp"))
# Какие события обслуживания режут историю на эпохи (сбрасывают базлайн).
# fan_curve_change/undervolt_change — смена режима, её ловит CUSUM (§5.5).
EPOCH_RESET_KINDS = ["repaste", "cleaning", "repad", "hw_change"]


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:  # noqa: BLE001 — кривой tz в БД не должен валить воркер
        return ZoneInfo("UTC")


def _day_bounds(day: dt.date, tz: ZoneInfo) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    return start.astimezone(dt.UTC), (start + dt.timedelta(days=1)).astimezone(dt.UTC)


async def _load_series(
    conn: asyncpg.Connection, device_id: uuid.UUID,
    t0: dt.datetime, t1: dt.datetime,
) -> dict[str, np.ndarray]:
    rows = await conn.fetch(
        """SELECT extract(epoch FROM ts)::float8 AS ts,
                  cpu_power, cpu_temp, gpu_power, gpu_temp, fan_rpm
           FROM telemetry_raw
           WHERE device_id = $1 AND ts >= $2 AND ts < $3
           ORDER BY ts""",
        device_id, t0, t1,
    )
    def col(name: str) -> np.ndarray:
        return np.array([r[name] if r[name] is not None else np.nan for r in rows], dtype=float)

    return {k: col(k) for k in ("ts", "cpu_power", "cpu_temp", "gpu_power", "gpu_temp", "fan_rpm")}


async def _active_devices(conn: asyncpg.Connection, since_days: int = 3) -> list[asyncpg.Record]:
    return await conn.fetch(
        """SELECT id, timezone, analysis_overrides FROM devices
           WHERE last_seen_at > now() - make_interval(days => $1)""",
        since_days,
    )


# ---------------------------------------------------------------- T_ambient --

async def estimate_ambient_for_device_day(
    conn: asyncpg.Connection, device_id: uuid.UUID,
    day: dt.date, tz: ZoneInfo, params: AnalysisParams,
) -> bool:
    t0, t1 = _day_bounds(day, tz)
    series = await _load_series(conn, device_id, t0, t1)
    if series["ts"].size == 0:
        return False
    episodes = ambient_mod.find_idle_episodes(
        series["ts"], series["cpu_power"], series["cpu_temp"], params
    )
    estimate = ambient_mod.estimate_day_ambient(episodes, params)
    if estimate is None:
        return False
    await conn.execute(
        """INSERT INTO ambient_estimates
               (device_id, day, t_ambient, confidence, idle_minutes, episodes_n,
                method, model_version)
           VALUES ($1, $2, $3, $4, $5, $6, 'idle_p10_v1', $7)
           ON CONFLICT (device_id, day) DO UPDATE SET
               t_ambient = EXCLUDED.t_ambient, confidence = EXCLUDED.confidence,
               idle_minutes = EXCLUDED.idle_minutes, episodes_n = EXCLUDED.episodes_n,
               method = EXCLUDED.method, model_version = EXCLUDED.model_version""",
        device_id, day, estimate.t_ambient, estimate.confidence,
        estimate.idle_minutes, estimate.episodes_n, MODEL_VERSION,
    )
    return True


async def estimate_ambient_job(pool: asyncpg.Pool, params: AnalysisParams) -> None:
    """Сегодня + вчера (локальные дни устройства) для активных устройств;
    upsert идемпотентен, поэтому джоб можно гонять хоть каждый час."""
    async with pool.acquire() as conn:
        devices = await _active_devices(conn)
        for device in devices:
            tz = _tz(device["timezone"])
            dev_params = params.with_overrides(device["analysis_overrides"])
            today = dt.datetime.now(tz).date()
            updated = 0
            for day in (today - dt.timedelta(days=1), today):
                if await estimate_ambient_for_device_day(
                    conn, device["id"], day, tz, dev_params
                ):
                    updated += 1
            if updated:
                log.info("ambient.updated", device_id=str(device["id"]), days=updated)


async def _ambient_lookup(
    conn: asyncpg.Connection, device_id: uuid.UUID, params: AnalysisParams
) -> dict[dt.date, tuple[float, float]]:
    """Последние оценки → {день: (t_ambient, confidence)}; carry-forward с
    затуханием confidence делает ambient_for_day ниже."""
    rows = await conn.fetch(
        """SELECT day, t_ambient, confidence FROM ambient_estimates
           WHERE device_id = $1 ORDER BY day DESC LIMIT 30""",
        device_id,
    )
    return {r["day"]: (r["t_ambient"], r["confidence"]) for r in rows}


def ambient_for_day(
    estimates: dict[dt.date, tuple[float, float]], day: dt.date, params: AnalysisParams
) -> tuple[float, float] | None:
    for age in range(params.ambient_max_age_days + 1):
        hit = estimates.get(day - dt.timedelta(days=age))
        if hit is not None:
            t_amb, conf = hit
            return t_amb, conf * (params.ambient_decay_per_day ** age)
    return None


# ------------------------------------------------------------- окна и Rth --

async def detect_windows_for_device(
    conn: asyncpg.Connection, device: asyncpg.Record,
    t0: dt.datetime, t1: dt.datetime, params: AnalysisParams,
) -> tuple[int, dict[str, float]]:
    """Детекция окон + Rth на [t0, t1) для обоих доменов. Возвращает (число
    сохранённых окон, {domain: epoch-начало сессии, открытой на краю t1}).
    Окна без доступной ambient-оценки пропускаются (появятся при reprocess
    после первой ночи устройства)."""
    device_id = device["id"]
    tz = _tz(device["timezone"])
    series = await _load_series(conn, device_id, t0, t1)
    if series["ts"].size == 0:
        return 0, {}
    estimates = await _ambient_lookup(conn, device_id, params)
    saved = 0
    open_since: dict[str, float] = {}

    for domain, power_col, temp_col in DOMAINS:
        windows, rejected, open_ts = detect_stable_windows(
            series["ts"], series[power_col], series[temp_col], series["fan_rpm"],
            params, until_s=t1.timestamp(),
        )
        if open_ts is not None:
            open_since[domain] = open_ts
        if rejected:
            log.debug("windows.rejected", device_id=str(device_id), domain=domain,
                      reasons=dict(rejected))
        for w in windows:
            day = dt.datetime.fromtimestamp(w.start_ts, tz=dt.UTC).astimezone(tz).date()
            amb = ambient_for_day(estimates, day, params)
            if amb is None:
                log.info("windows.no_ambient", device_id=str(device_id), domain=domain)
                continue
            for point in attach_rth([w], amb[0], amb[1], params):
                await conn.execute(
                    """INSERT INTO rth_windows
                           (device_id, domain, window_start, window_end, duration_s,
                            p_tail, p_cv, t_tail, dtdt_tail, fan_rpm_avg,
                            t_ambient, ambient_confidence, rth, stratum,
                            quality, model_version)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                       ON CONFLICT (device_id, domain, window_start) DO UPDATE SET
                           window_end = EXCLUDED.window_end,
                           duration_s = EXCLUDED.duration_s,
                           p_tail = EXCLUDED.p_tail, p_cv = EXCLUDED.p_cv,
                           t_tail = EXCLUDED.t_tail, dtdt_tail = EXCLUDED.dtdt_tail,
                           fan_rpm_avg = EXCLUDED.fan_rpm_avg,
                           t_ambient = EXCLUDED.t_ambient,
                           ambient_confidence = EXCLUDED.ambient_confidence,
                           rth = EXCLUDED.rth, stratum = EXCLUDED.stratum,
                           quality = EXCLUDED.quality,
                           model_version = EXCLUDED.model_version""",
                    device_id, domain,
                    dt.datetime.fromtimestamp(w.start_ts, tz=dt.UTC),
                    dt.datetime.fromtimestamp(w.end_ts, tz=dt.UTC),
                    int(w.duration_s), float(w.p_tail), float(w.p_cv),
                    float(w.t_tail), float(w.dtdt_tail),
                    float(w.rpm_avg) if w.rpm_avg is not None else None,
                    float(point.t_ambient), float(point.ambient_confidence),
                    float(point.rth), point.stratum, float(point.quality),
                    MODEL_VERSION,
                )
                saved += 1
    return saved, open_since


async def detect_windows_job(pool: asyncpg.Pool, params: AnalysisParams) -> None:
    """Инкрементальный проход: [watermark − LOOKBACK, now − WATERMARK).
    Эмитятся только закрытые окна; открытое на правом крае дозреет: его начало
    хранится в analytics_state.open_since, и следующий прогон перечитывает
    серию от него — чанки воспроизводятся с теми же ключами, upsert идемпотентен.
    Кап перечитки 24 ч: «вечная» сессия не должна раздувать каждый прогон."""
    now = dt.datetime.now(dt.UTC)
    until = now - dt.timedelta(seconds=WATERMARK_S)
    floor = now - dt.timedelta(hours=24)
    async with pool.acquire() as conn:
        for device in await _active_devices(conn):
            dev_params = params.with_overrides(device["analysis_overrides"])
            state = await conn.fetchrow(
                """SELECT windows_processed_until, open_since
                   FROM analytics_state WHERE device_id = $1""",
                device["id"],
            )
            watermark = state["windows_processed_until"] if state else None
            t0 = (watermark or floor) - dt.timedelta(seconds=LOOKBACK_S)
            prev_open = json.loads(state["open_since"]) if state and state["open_since"] else {}
            if prev_open:
                oldest = dt.datetime.fromtimestamp(min(prev_open.values()), dt.UTC)
                t0 = max(min(t0, oldest), floor)
            saved, open_since = await detect_windows_for_device(
                conn, device, t0, until, dev_params)
            await conn.execute(
                """INSERT INTO analytics_state (device_id, windows_processed_until, open_since)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (device_id) DO UPDATE
                       SET windows_processed_until = EXCLUDED.windows_processed_until,
                           open_since = EXCLUDED.open_since""",
                device["id"], until, json.dumps(open_since),
            )
            if saved:
                log.info("windows.saved", device_id=str(device["id"]), n=saved)


# ------------------------------------------------------------------ тренд --

async def update_trends_for_device(
    conn: asyncpg.Connection, device: asyncpg.Record, params: AnalysisParams
) -> None:
    device_id = device["id"]
    tz_name = device["timezone"] or "UTC"
    maintenance_epoch = await conn.fetchval(
        """SELECT max(ts) FROM maintenance_events
           WHERE device_id = $1 AND kind = ANY($2::text[])""",
        device_id, EPOCH_RESET_KINDS,
    )

    for domain in ("cpu", "gpu"):
        # эпоха: последний repaste/hw_change, иначе — начало данных домена (§5.5)
        epoch_start = maintenance_epoch or await conn.fetchval(
            "SELECT min(window_start) FROM rth_windows WHERE device_id = $1 AND domain = $2",
            device_id, domain,
        )
        if epoch_start is None:
            continue
        # headline-страта: где больше всего окон за trend_window_days
        stratum = await conn.fetchval(
            """SELECT stratum FROM rth_windows
               WHERE device_id = $1 AND domain = $2 AND quality >= $3
                 AND window_start >= greatest(
                     $4::timestamptz, now() - make_interval(days => $5))
               GROUP BY stratum ORDER BY count(*) DESC LIMIT 1""",
            device_id, domain, params.quality_min, epoch_start, params.trend_window_days,
        )
        if stratum is None:
            # Эпоха есть (обслуживание/старые данные), окон в ней ещё нет:
            # сбрасываем снапшот, иначе health показывал бы цифры ПРОШЛОЙ эпохи.
            await conn.execute(
                """INSERT INTO device_health
                       (device_id, domain, computed_at, epoch_start, data_quality,
                        diagnosis, model_version)
                   VALUES ($1, $2, now(), $3, 'sparse', 'insufficient_data', $4)
                   ON CONFLICT (device_id, domain) DO UPDATE SET
                       computed_at = now(), epoch_start = EXCLUDED.epoch_start,
                       rth_baseline = NULL, rth_current = NULL,
                       degradation_pct = NULL, slope_mkw_per_30d = NULL,
                       slope_ci_low = NULL, slope_ci_high = NULL,
                       forecast_throttle_date = NULL, health_score = NULL,
                       data_quality = 'sparse', diagnosis = 'insufficient_data',
                       model_version = EXCLUDED.model_version""",
                device_id, domain, epoch_start, MODEL_VERSION,
            )
            continue

        daily = await conn.fetch(
            """SELECT (window_start AT TIME ZONE $5)::date AS day,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY rth) AS med,
                      count(*) AS n
               FROM rth_windows
               WHERE device_id = $1 AND domain = $2 AND stratum = $3
                 AND quality >= $4
                 AND window_start >= greatest(
                     $6::timestamptz, now() - make_interval(days => $7))
               GROUP BY day ORDER BY day""",
            device_id, domain, stratum, params.quality_min, tz_name, epoch_start,
            params.trend_window_days,
        )
        if not daily:
            continue
        total_windows = sum(r["n"] for r in daily)
        days = np.array([(r["day"] - daily[0]["day"]).days for r in daily], dtype=float)
        medians = np.array([r["med"] for r in daily], dtype=float)

        # CUSUM: ступенька (смена режима) ≠ деградация — тренд только после неё
        changepoints = trend_mod.cusum_changepoints(
            medians, params.cusum_k_sigma, params.cusum_h_sigma
        )
        seg = changepoints[-1] if changepoints else 0
        if changepoints:
            # §5.5: чейнджпойнт ⇒ предложение подтвердить событие обслуживания.
            # Эпоху НЕ режет (kind не в EPOCH_RESET_KINDS) — псевдо-граница
            # тренда уже обеспечена сегментом seg; базлайн ждёт подтверждения.
            step_pct = 100.0 * (medians[seg] - medians[seg - 1]) / medians[seg - 1]
            await _suggest_regime_change(
                conn, device_id, domain, stratum,
                daily[seg]["day"], step_pct, _tz(tz_name),
            )

        data_quality = "ok"
        trend = None
        enough = (len(daily) - seg >= params.publish_min_days
                  and total_windows >= params.publish_min_windows)
        if enough:
            trend = trend_mod.theil_sen(days[seg:], medians[seg:])
        elif changepoints:
            data_quality = "regime_change"
        else:
            data_quality = "sparse"

        baseline = await conn.fetchval(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY rth)
               FROM rth_windows
               WHERE device_id = $1 AND domain = $2 AND stratum = $3 AND quality >= $4
                 AND window_start >= $5::timestamptz
                 AND window_start < $5::timestamptz + make_interval(days => $6)
               HAVING count(*) >= $7""",
            device_id, domain, stratum, params.quality_min,
            epoch_start, params.baseline_days, params.baseline_min_windows,
        )
        # фильтр по эпохе обязателен: сразу после repaste в «последних 7 днях»
        # ещё лежат окна ПРОШЛОЙ эпохи — без него degradation_pct сравнивал бы
        # новый базлайн со смесью двух паст (§5.5: уровень живёт внутри эпохи)
        current = await conn.fetchval(
            """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY rth)
               FROM rth_windows
               WHERE device_id = $1 AND domain = $2 AND stratum = $3 AND quality >= $4
                 AND window_start >= now() - make_interval(days => $5)
                 AND window_start >= $6::timestamptz""",
            device_id, domain, stratum, params.quality_min, params.current_days,
            epoch_start,
        )

        degradation_pct = None
        if baseline and current:
            degradation_pct = 100.0 * (current - baseline) / baseline

        forecast_date = None
        slope_mkw = ci_low = ci_high = None
        if trend is not None:
            scale = 30.0 * 1000.0  # K/W в день → мК/Вт за 30 дней
            slope_mkw, ci_low, ci_high = (
                trend.slope_per_day * scale, trend.ci_low * scale, trend.ci_high * scale)
            if trend.significant and current is not None:
                p_typ = await conn.fetchval(
                    """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY p_tail)
                       FROM rth_windows
                       WHERE device_id = $1 AND domain = $2 AND stratum = $3
                         AND window_start >= now() - interval '30 days'
                         AND window_start >= $4::timestamptz""",
                    device_id, domain, stratum, epoch_start,
                )
                t_amb_typ = await conn.fetchval(
                    """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY t_ambient)
                       FROM ambient_estimates
                       WHERE device_id = $1 AND day >= current_date - 30""",
                    device_id,
                )
                if p_typ and t_amb_typ is not None:
                    days_left = trend_mod.forecast_days_to_throttle(
                        current, trend.slope_per_day, p_typ, t_amb_typ, params)
                    if days_left is not None:
                        forecast_date = (
                            dt.datetime.now(dt.UTC) + dt.timedelta(days=days_left)).date()

        diagnosis = await _diagnose(conn, device_id, domain, stratum, epoch_start, params)

        penalty = {"ok": 0.0, "sparse": 0.5, "regime_change": 0.4}.get(data_quality, 0.5)
        days_left_num = None
        if forecast_date is not None:
            days_left_num = (forecast_date - dt.date.today()).days
        score = trend_mod.health_score(degradation_pct, days_left_num, penalty)

        await conn.execute(
            """INSERT INTO device_health
                   (device_id, domain, computed_at, epoch_start, rth_baseline,
                    rth_current, degradation_pct, slope_mkw_per_30d,
                    slope_ci_low, slope_ci_high, forecast_throttle_date,
                    health_score, data_quality, diagnosis, model_version)
               VALUES ($1,$2,now(),$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (device_id, domain) DO UPDATE SET
                   computed_at = now(), epoch_start = EXCLUDED.epoch_start,
                   rth_baseline = EXCLUDED.rth_baseline,
                   rth_current = EXCLUDED.rth_current,
                   degradation_pct = EXCLUDED.degradation_pct,
                   slope_mkw_per_30d = EXCLUDED.slope_mkw_per_30d,
                   slope_ci_low = EXCLUDED.slope_ci_low,
                   slope_ci_high = EXCLUDED.slope_ci_high,
                   forecast_throttle_date = EXCLUDED.forecast_throttle_date,
                   health_score = EXCLUDED.health_score,
                   data_quality = EXCLUDED.data_quality,
                   diagnosis = EXCLUDED.diagnosis,
                   model_version = EXCLUDED.model_version""",
            device_id, domain, epoch_start,
            baseline, current, degradation_pct,
            slope_mkw, ci_low, ci_high, forecast_date, score,
            data_quality, diagnosis, MODEL_VERSION,
        )


async def _suggest_regime_change(
    conn: asyncpg.Connection, device_id: uuid.UUID, domain: str,
    stratum: str, day: dt.date, step_pct: float, tz: ZoneInfo,
) -> None:
    """Предложение из CUSUM (§5.5): «около этого дня сменился режим —
    подтвердите, что это было» (чистка / кривая вентиляторов / андервольт).

    Дедуп — NOT EXISTS в ±3 дня: оценка дня ступеньки дрожит на ±1 день по
    мере накопления данных, и cpu/gpu обычно стреляют от одной физической
    причины — второй строки про то же событие быть не должно. Подтверждение
    юзер делает обычным POST /maintenance с настоящим видом работ."""
    ts = dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(dt.UTC)
    direction = "вверх" if step_pct > 0 else "вниз"
    note = (f"CUSUM: ступенька дневной медианы Rth {direction} на "
            f"{abs(step_pct):.1f}% ({domain}/{stratum}) около {day.isoformat()}. "
            f"Подтвердите событие: чистка, кривая вентиляторов, андервольт?")
    status = await conn.execute(
        """INSERT INTO maintenance_events (device_id, ts, kind, source, note)
           SELECT $1, $2, 'regime_change', 'changepoint_suggested', $3
           WHERE NOT EXISTS (
               SELECT 1 FROM maintenance_events
               WHERE device_id = $1 AND source = 'changepoint_suggested'
                 AND ts BETWEEN $2::timestamptz - interval '3 days'
                            AND $2::timestamptz + interval '3 days')""",
        device_id, ts, note,
    )
    if status.endswith("1"):
        log.info("trends.changepoint_suggested", device_id=str(device_id),
                 domain=domain, day=str(day), step_pct=round(step_pct, 1))


async def _diagnose(
    conn: asyncpg.Connection, device_id: uuid.UUID, domain: str,
    stratum: str, epoch_start: dt.datetime, params: AnalysisParams,
) -> str:
    # $5::timestamptz обязателен: без каста PG выводит тип $5 из выражения
    # «$5 + make_interval(...)» как interval, и сравнение с window_start падает
    rows = await conn.fetch(
        """SELECT rth, fan_rpm_avg,
                  window_start < $5::timestamptz + make_interval(days => $6) AS is_early,
                  window_start >= now() - make_interval(days => $6) AS is_late
           FROM rth_windows
           WHERE device_id = $1 AND domain = $2 AND stratum = $3
             AND quality >= $4 AND window_start >= $5::timestamptz""",
        device_id, domain, stratum, params.quality_min, epoch_start, params.baseline_days,
    )

    def arrays(flag: str) -> tuple[np.ndarray, np.ndarray]:
        sel = [r for r in rows if r[flag]]
        rth = np.array([r["rth"] for r in sel], dtype=float)
        rpm = np.array(
            [r["fan_rpm_avg"] if r["fan_rpm_avg"] is not None else np.nan for r in sel],
            dtype=float,
        )
        return rth, rpm

    rth_early, rpm_early = arrays("is_early")
    rth_late, rpm_late = arrays("is_late")
    return trend_mod.diagnose(rth_early, rpm_early, rth_late, rpm_late, params)


async def update_trends_job(pool: asyncpg.Pool, params: AnalysisParams) -> None:
    async with pool.acquire() as conn:
        devices = await conn.fetch(
            """SELECT d.id, d.timezone, d.analysis_overrides FROM devices d
               WHERE EXISTS (SELECT 1 FROM rth_windows w WHERE w.device_id = d.id)""",
        )
        for device in devices:
            await update_trends_for_device(
                conn, device, params.with_overrides(device["analysis_overrides"])
            )
        if devices:
            log.info("trends.updated", devices=len(devices))


# ------------------------------------------------------- reprocess/cleanup --

async def reprocess_job(pool: asyncpg.Pool, params: AnalysisParams) -> None:
    """Опоздавшие данные (дренаж спула агента): пересчёт ambient затронутых
    дней и окон затронутого диапазона (architecture.md §4.4)."""
    async with pool.acquire() as conn:
        # device_id СОЗНАТЕЛЬНО алиасится в id: detect_windows_for_device
        # читает device["id"] — без алиаса туда уезжал int-id строки очереди
        entries = await conn.fetch(
            """SELECT q.id AS queue_id, q.device_id AS id,
                      q.range_start, q.range_end,
                      d.timezone, d.analysis_overrides
               FROM reprocess_queue q JOIN devices d ON d.id = q.device_id
               WHERE q.processed_at IS NULL
               ORDER BY q.enqueued_at LIMIT 100""",
        )
        for entry in entries:
            dev_params = params.with_overrides(entry["analysis_overrides"])
            tz = _tz(entry["timezone"])
            day = entry["range_start"].astimezone(tz).date()
            last_day = entry["range_end"].astimezone(tz).date()
            while day <= last_day:
                await estimate_ambient_for_device_day(
                    conn, entry["id"], day, tz, dev_params)
                day += dt.timedelta(days=1)
            pad = dt.timedelta(seconds=REPROCESS_PAD_S)
            t0, t1 = entry["range_start"] - pad, entry["range_end"] + pad
            async with conn.transaction():
                # Пересчёт отрезка = детерминированная функция от raw (§4.4):
                # старые окна внутри диапазона выкидываем, а не апсертим —
                # у окна, чья граница сдвинулась на полных данных, другой
                # window_start, и upsert оставил бы рядом стухший дубль
                # (наблюдалось живьём: пересечения от инкрементальных прогонов)
                await conn.execute(
                    """DELETE FROM rth_windows
                       WHERE device_id = $1
                         AND window_start >= $2 AND window_end <= $3""",
                    entry["id"], t0, t1,
                )
                await detect_windows_for_device(conn, entry, t0, t1, dev_params)
            await conn.execute(
                "UPDATE reprocess_queue SET processed_at = now() WHERE id = $1",
                entry["queue_id"])
        if entries:
            log.info("reprocess.done", ranges=len(entries))


async def cleanup_job(pool: asyncpg.Pool, params: AnalysisParams) -> None:
    """Ночная чистка обычных таблиц (гипертаблицы чистит retention-политика)."""
    async with pool.acquire() as conn:
        batches = await conn.execute(
            "DELETE FROM ingest_batches WHERE received_at < now() - interval '30 days'")
        queue = await conn.execute(
            "DELETE FROM reprocess_queue WHERE processed_at < now() - interval '7 days'")
        log.info("cleanup.done", ingest_batches=batches, reprocess_queue=queue)
