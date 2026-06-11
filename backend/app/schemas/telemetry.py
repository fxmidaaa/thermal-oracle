"""Wire-контракт агент → бэкенд (architecture.md §3).

Pydantic проверяет только СТРУКТУРУ (типы, размеры, tz-aware метки).
Физические диапазоны значений сюда не входят намеренно: значение вне диапазона
не повод отклонять батч — поле обнуляется с флагом в services/ingest_service.

extra="forbid": новые поля требуют bump schema_version — дисциплина контракта;
бэкенд поддерживает версии N и N-1. Агент зеркалит эти модели у себя
(agent/thermal_agent/models.py), совместимость держат contract-тесты.
"""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat

MAX_BATCH_SAMPLES = 120  # 30с штатно; до 120 при дренаже оффлайн-спула


class TelemetrySample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: AwareDatetime
    cpu_temp: FiniteFloat | None = None
    gpu_temp: FiniteFloat | None = None
    cpu_power: FiniteFloat | None = None
    gpu_power: FiniteFloat | None = None
    fan_rpm: int | None = None
    process: str | None = Field(default=None, max_length=256)


class TelemetryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    batch_id: UUID
    sent_at: AwareDatetime
    agent_version: str = Field(max_length=32)
    samples: list[TelemetrySample] = Field(min_length=1, max_length=MAX_BATCH_SAMPLES)


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    status: Literal["ok", "duplicate"] = "ok"
