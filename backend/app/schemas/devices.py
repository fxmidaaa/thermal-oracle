import datetime as dt
import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    platform: str
    device_class: str
    agent_version: str | None
    last_seen_at: dt.datetime | None
    created_at: dt.datetime


class PairingCodeOut(BaseModel):
    code: str            # показывается один раз; в БД — только SHA-256
    expires_at: dt.datetime


class PairRequest(BaseModel):
    """Агент → бэкенд при сопряжении. Имя/платформу знает агент (hostname)."""
    code: str = Field(min_length=4, max_length=16)
    name: str = Field(min_length=1, max_length=64)
    platform: Literal["windows", "macos"]
    device_class: Literal["laptop", "desktop"] = "laptop"
    agent_version: str | None = Field(default=None, max_length=32)


class PairResponse(BaseModel):
    device_id: uuid.UUID
    device_token: str    # постоянный Bearer агента; показывается один раз


class HealthOut(BaseModel):
    domain: str
    computed_at: dt.datetime
    epoch_start: dt.datetime | None
    rth_baseline: float | None
    rth_current: float | None
    degradation_pct: float | None
    slope_mkw_per_30d: float | None
    slope_ci_low: float | None
    slope_ci_high: float | None
    forecast_throttle_date: dt.date | None
    health_score: int | None
    data_quality: str
    diagnosis: str


class TrendPoint(BaseModel):
    day: dt.date
    rth_median: float
    rth_p25: float
    rth_p75: float
    windows_n: int


class TrendOut(BaseModel):
    domain: str
    stratum: str
    points: list[TrendPoint]
    rth_baseline: float | None = None
    rth_current: float | None = None
    slope_mkw_per_30d: float | None = None
    slope_ci_low: float | None = None
    slope_ci_high: float | None = None
    data_quality: str | None = None


class MaintenanceRequest(BaseModel):
    """Событие обслуживания. Все три типа режут историю на эпохи: базлайн и
    тренд деградации считаются заново от performed_at (architecture.md §5.5)."""
    maintenance_type: Literal["paste_replacement", "dust_cleaning", "repad"]
    performed_at: AwareDatetime
    notes: str | None = Field(default=None, max_length=500)
    # тип термоинтерфейса — для приоров деградации (v2); опционально
    tim_type: Literal["paste", "liquid_metal", "ptm7950", "stock", "unknown"] | None = None


class MaintenanceOut(BaseModel):
    id: int
    maintenance_type: str
    performed_at: dt.datetime
    notes: str | None
    tim_type: str | None
    source: str


class TimeseriesPoint(BaseModel):
    bucket: dt.datetime
    cpu_temp_avg: float | None
    cpu_temp_max: float | None
    gpu_temp_avg: float | None
    gpu_temp_max: float | None
    cpu_power_avg: float | None
    cpu_power_max: float | None
    gpu_power_avg: float | None
    gpu_power_max: float | None
    fan_rpm_avg: float | None
    fan_rpm_max: float | None
    n: int
