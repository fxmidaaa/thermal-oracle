"""Витрина данных для фронтенда: устройства, паринг-коды, health/trend/timeseries.
Каждый device-эндпоинт идёт через get_owned_device — строгая тенантность."""
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.analytics.jobs import update_trends_for_device
from app.analytics.params import AnalysisParams
from app.api.v1.deps import get_current_user, get_owned_device
from app.schemas.devices import (
    CurrentHealthOut,
    DeviceOut,
    DomainCurrentOut,
    HealthOut,
    MaintenanceOut,
    MaintenanceRequest,
    MaintenanceSuggestionOut,
    PairingCodeOut,
    RthHistoryOut,
    RthHistoryPoint,
    TimeseriesPoint,
    TrendOut,
    TrendPoint,
)
from app.services.pairing_service import create_pairing_code

router = APIRouter(prefix="/devices", tags=["devices"])

# API-типы ↔ виды в журнале (БД знает больше видов: fan_curve_change и т.п.)
_MAINT_TYPE_TO_KIND = {
    "paste_replacement": "repaste",
    "dust_cleaning": "cleaning",
    "repad": "repad",
}
_KIND_TO_MAINT_TYPE = {v: k for k, v in _MAINT_TYPE_TO_KIND.items()}

_TS_TABLES = {"1m": "telemetry_1m", "1h": "telemetry_1h"}
_TS_DEFAULT = {"1m": dt.timedelta(hours=24), "1h": dt.timedelta(days=7)}
_TS_MAX = {"1m": dt.timedelta(hours=48), "1h": dt.timedelta(days=90)}


def _days_to(date: dt.date | None) -> int | None:
    return (date - dt.date.today()).days if date is not None else None


async def _device_quality_gate(conn, device_id: uuid.UUID) -> float:
    """Эффективный трендовый гейт качества: дефолт + devices.analysis_overrides
    (тот же путь, что в analytics-джобах, — фронт видит ровно те точки,
    которые видит тренд)."""
    overrides = await conn.fetchval(
        "SELECT analysis_overrides FROM devices WHERE id = $1", device_id)
    return AnalysisParams().with_overrides(overrides).quality_min


@router.get("", response_model=list[DeviceOut])
async def list_devices(
    request: Request, user_id: uuid.UUID = Depends(get_current_user)
) -> list[DeviceOut]:
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, name, platform, device_class, agent_version,
                      last_seen_at, created_at
               FROM devices WHERE user_id = $1 ORDER BY created_at""",
            user_id,
        )
    return [DeviceOut(**dict(r)) for r in rows]


@router.post("/pairing-code", response_model=PairingCodeOut, status_code=201)
async def issue_pairing_code(
    request: Request, user_id: uuid.UUID = Depends(get_current_user)
) -> PairingCodeOut:
    """Код для `thermal-agent pair`: одноразовый, живёт 10 минут."""
    async with request.app.state.pool.acquire() as conn:
        code, expires_at = await create_pairing_code(
            conn, user_id, request.app.state.settings.pairing_ttl_minutes
        )
    return PairingCodeOut(code=code, expires_at=expires_at)


@router.post("/{device_id}/maintenance", response_model=MaintenanceOut, status_code=201)
async def log_maintenance(
    body: MaintenanceRequest,
    request: Request,
    device_id: uuid.UUID = Depends(get_owned_device),
) -> MaintenanceOut:
    """Записать обслуживание. Любой из трёх типов начинает НОВУЮ эпоху:
    базлайн и тренд Rth считаются заново от performed_at; старые точки
    остаются в журнале, но в деградацию текущей эпохи не входят.
    device_health пересчитывается сразу, не дожидаясь тика воркера."""
    if body.performed_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="performed_at is in the future")

    kind = _MAINT_TYPE_TO_KIND[body.maintenance_type]
    async with request.app.state.pool.acquire() as conn:
        event_id = await conn.fetchval(
            """INSERT INTO maintenance_events (device_id, ts, kind, tim_type, source, note)
               VALUES ($1, $2, $3, $4, 'user', $5) RETURNING id""",
            device_id, body.performed_at, kind, body.tim_type, body.notes,
        )
        device = await conn.fetchrow(
            "SELECT id, timezone, analysis_overrides FROM devices WHERE id = $1", device_id
        )
        params = AnalysisParams().with_overrides(device["analysis_overrides"])
        await update_trends_for_device(conn, device, params)

    return MaintenanceOut(
        id=event_id, maintenance_type=body.maintenance_type,
        performed_at=body.performed_at, notes=body.notes,
        tim_type=body.tim_type, source="user",
    )


@router.get("/{device_id}/maintenance", response_model=list[MaintenanceOut])
async def list_maintenance(
    request: Request, device_id: uuid.UUID = Depends(get_owned_device)
) -> list[MaintenanceOut]:
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, ts, kind, tim_type, source, note
               FROM maintenance_events WHERE device_id = $1 ORDER BY ts DESC""",
            device_id,
        )
    return [
        MaintenanceOut(
            id=r["id"],
            maintenance_type=_KIND_TO_MAINT_TYPE.get(r["kind"], r["kind"]),
            performed_at=r["ts"], notes=r["note"],
            tim_type=r["tim_type"], source=r["source"],
        )
        for r in rows
    ]


@router.get(
    "/{device_id}/maintenance-suggestions",
    response_model=list[MaintenanceSuggestionOut],
)
async def list_maintenance_suggestions(
    request: Request, device_id: uuid.UUID = Depends(get_owned_device)
) -> list[MaintenanceSuggestionOut]:
    """Открытые CUSUM-предложения «подтвердите смену режима» (§5.5).
    Подтверждение — обычный POST /maintenance с performed_at ≈ suggested_at:
    user-событие в ±3 дня закрывает предложение (то же окно, что у дедупа
    самих предложений), отдельного write-эндпоинта не нужно."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.id, s.ts, s.note
               FROM maintenance_events s
               WHERE s.device_id = $1 AND s.source = 'changepoint_suggested'
                 AND NOT EXISTS (
                     SELECT 1 FROM maintenance_events u
                     WHERE u.device_id = s.device_id AND u.source = 'user'
                       AND u.ts BETWEEN s.ts - interval '3 days'
                                    AND s.ts + interval '3 days')
               ORDER BY s.ts DESC""",
            device_id,
        )
    return [
        MaintenanceSuggestionOut(id=r["id"], suggested_at=r["ts"], note=r["note"])
        for r in rows
    ]


@router.get("/{device_id}/health", response_model=list[HealthOut])
async def device_health(
    request: Request, device_id: uuid.UUID = Depends(get_owned_device)
) -> list[HealthOut]:
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT domain, computed_at, epoch_start, rth_baseline, rth_current,
                      degradation_pct, slope_mkw_per_30d, slope_ci_low, slope_ci_high,
                      forecast_throttle_date, health_score, data_quality, diagnosis
               FROM device_health WHERE device_id = $1 ORDER BY domain""",
            device_id,
        )
    return [
        HealthOut(**dict(r), days_to_critical=_days_to(r["forecast_throttle_date"]))
        for r in rows
    ]


@router.get("/{device_id}/current-health", response_model=CurrentHealthOut)
async def device_current_health(
    request: Request, device_id: uuid.UUID = Depends(get_owned_device)
) -> CurrentHealthOut:
    """Шапка дашборда одним запросом: последняя ambient-калибровка + по каждому
    домену последняя честная Rth-точка и снапшот health. Домены без единой
    точки и без снапшота не возвращаются; новое устройство → domains=[]."""
    async with request.app.state.pool.acquire() as conn:
        gate = await _device_quality_gate(conn, device_id)
        ambient = await conn.fetchrow(
            """SELECT day, t_ambient, confidence FROM ambient_estimates
               WHERE device_id = $1 ORDER BY day DESC LIMIT 1""",
            device_id,
        )
        latest = {
            r["domain"]: r
            for r in await conn.fetch(
                """SELECT DISTINCT ON (domain) domain, rth, window_start
                   FROM rth_windows
                   WHERE device_id = $1 AND quality >= $2
                   ORDER BY domain, window_start DESC""",
                device_id, gate,
            )
        }
        health = {
            r["domain"]: r
            for r in await conn.fetch(
                """SELECT domain, rth_current, health_score, data_quality,
                          forecast_throttle_date
                   FROM device_health WHERE device_id = $1""",
                device_id,
            )
        }

    domains = []
    for domain in ("cpu", "gpu"):
        if domain not in latest and domain not in health:
            continue
        win, snap = latest.get(domain), health.get(domain)
        domains.append(DomainCurrentOut(
            domain=domain,
            rth_latest=win["rth"] if win else None,
            rth_latest_at=win["window_start"] if win else None,
            rth_current=snap["rth_current"] if snap else None,
            health_score=snap["health_score"] if snap else None,
            data_quality=snap["data_quality"] if snap else None,
            days_to_critical=_days_to(snap["forecast_throttle_date"]) if snap else None,
        ))
    return CurrentHealthOut(
        t_ambient=ambient["t_ambient"] if ambient else None,
        ambient_confidence=ambient["confidence"] if ambient else None,
        ambient_day=ambient["day"] if ambient else None,
        domains=domains,
    )


@router.get("/{device_id}/rth-history", response_model=RthHistoryOut)
async def device_rth_history(
    request: Request,
    device_id: uuid.UUID = Depends(get_owned_device),
    domain: str = Query("cpu", pattern="^(cpu|gpu)$"),
    stratum: str | None = Query(None, pattern="^(p35_50|p50_80|p80plus)$"),
    days: int = Query(30, ge=1, le=90),
) -> RthHistoryOut:
    """Скаттер отдельных Rth-окон для графика — только точки, прошедшие
    per-device трендовый гейт качества. Дневные агрегаты и длинные горизонты —
    GET /trend. Нет данных → честный пустой points (дашборд это переживает)."""
    async with request.app.state.pool.acquire() as conn:
        gate = await _device_quality_gate(conn, device_id)
        rows = await conn.fetch(
            """SELECT window_start, duration_s, rth, p_tail, stratum, quality,
                      fan_rpm_avg
               FROM rth_windows
               WHERE device_id = $1 AND domain = $2 AND quality >= $3
                 AND window_start >= now() - make_interval(days => $4)
                 AND ($5::text IS NULL OR stratum = $5)
               ORDER BY window_start""",
            device_id, domain, gate, days, stratum,
        )
    return RthHistoryOut(
        domain=domain, days=days, quality_gate=gate,
        points=[RthHistoryPoint(**dict(r)) for r in rows],
    )


@router.get("/{device_id}/trend", response_model=TrendOut)
async def device_trend(
    request: Request,
    device_id: uuid.UUID = Depends(get_owned_device),
    domain: str = Query("cpu", pattern="^(cpu|gpu)$"),
    stratum: str | None = Query(None, pattern="^(p35_50|p50_80|p80plus)$"),
    days: int = Query(90, ge=7, le=365),
) -> TrendOut:
    """Дневные точки Rth (медиана + межквартильная полоса) + параметры тренда
    из device_health. Без stratum берётся headline-страта (где больше окон)."""
    async with request.app.state.pool.acquire() as conn:
        if stratum is None:
            stratum = await conn.fetchval(
                """SELECT stratum FROM rth_windows
                   WHERE device_id = $1 AND domain = $2
                     AND window_start >= now() - make_interval(days => $3)
                   GROUP BY stratum ORDER BY count(*) DESC LIMIT 1""",
                device_id, domain, days,
            )
            if stratum is None:
                raise HTTPException(status_code=404, detail="no rth data for device")

        tz = await conn.fetchval(
            "SELECT timezone FROM devices WHERE id = $1", device_id) or "UTC"
        rows = await conn.fetch(
            """SELECT (window_start AT TIME ZONE $5)::date AS day,
                      percentile_cont(0.5)  WITHIN GROUP (ORDER BY rth) AS med,
                      percentile_cont(0.25) WITHIN GROUP (ORDER BY rth) AS p25,
                      percentile_cont(0.75) WITHIN GROUP (ORDER BY rth) AS p75,
                      count(*) AS n
               FROM rth_windows
               WHERE device_id = $1 AND domain = $2 AND stratum = $3
                 AND window_start >= now() - make_interval(days => $4)
               GROUP BY day ORDER BY day""",
            device_id, domain, stratum, days, tz,
        )
        health = await conn.fetchrow(
            """SELECT rth_baseline, rth_current, slope_mkw_per_30d,
                      slope_ci_low, slope_ci_high, data_quality
               FROM device_health WHERE device_id = $1 AND domain = $2""",
            device_id, domain,
        )

    points = [
        TrendPoint(day=r["day"], rth_median=r["med"], rth_p25=r["p25"],
                   rth_p75=r["p75"], windows_n=r["n"])
        for r in rows
    ]
    return TrendOut(
        domain=domain, stratum=stratum, points=points,
        **(dict(health) if health else {}),
    )


@router.get("/{device_id}/timeseries", response_model=list[TimeseriesPoint])
async def device_timeseries(
    request: Request,
    device_id: uuid.UUID = Depends(get_owned_device),
    bucket: str = Query("1m", pattern="^(1m|1h)$"),
    time_from: dt.datetime | None = Query(None, alias="from"),
    time_to: dt.datetime | None = Query(None, alias="to"),
) -> list[TimeseriesPoint]:
    """Исторические агрегаты из continuous aggregates (real-time режим:
    свежий немате­риализованный хвост дочитывается из raw на лету)."""
    now = dt.datetime.now(dt.UTC)
    time_to = time_to or now
    time_from = time_from or time_to - _TS_DEFAULT[bucket]
    if time_from >= time_to:
        raise HTTPException(status_code=422, detail="'from' must be before 'to'")
    if time_to - time_from > _TS_MAX[bucket]:
        raise HTTPException(
            status_code=422,
            detail=f"range too wide for bucket {bucket}; use coarser bucket",
        )

    table = _TS_TABLES[bucket]  # имя из белого списка, не из ввода
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT bucket, cpu_temp_avg, cpu_temp_max, gpu_temp_avg, gpu_temp_max,
                       cpu_power_avg, cpu_power_max, gpu_power_avg, gpu_power_max,
                       fan_rpm_avg, fan_rpm_max, n
                FROM {table}
                WHERE device_id = $1 AND bucket >= $2 AND bucket < $3
                ORDER BY bucket""",
            device_id, time_from, time_to,
        )
    return [TimeseriesPoint(**dict(r)) for r in rows]
