"""Wire-контракт агент → бэкенд: НАМЕРЕННАЯ копия backend/app/schemas/telemetry.py.

Агент не зависит от пакета backend (не тащим FastAPI/Pydantic на машины
пользователей); контракт — версионированный JSON, совместимость держат
contract-тесты backend/tests/contract/. Менять формат можно только синхронно
с bump SCHEMA_VERSION (бэкенд принимает N и N-1).
"""
import datetime as dt
import gzip
import json
import secrets
import time
import uuid
from dataclasses import dataclass

from thermal_agent import __version__ as AGENT_VERSION

SCHEMA_VERSION = 1
MAX_BATCH_SAMPLES = 120  # == app.schemas.telemetry.MAX_BATCH_SAMPLES (contract-тест)


def uuid7() -> uuid.UUID:
    """UUIDv7 (timestamp-ordered): batch_id с локальностью по времени для
    PK-индекса ingest_batches. В stdlib появится позже — 16 строк своих."""
    ms = time.time_ns() // 1_000_000
    raw = bytearray(ms.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant RFC 4122
    return uuid.UUID(bytes=bytes(raw))


@dataclass(slots=True)
class Sample:
    """Один сэмпл 1 Гц. None = сенсор не ответил (повторять прошлое значение
    запрещено: «замороженный» сенсор неотличим от плато и портит аналитику)."""

    ts_ms: int  # unix-время UTC, миллисекунды
    cpu_temp: float | None = None
    gpu_temp: float | None = None
    cpu_power: float | None = None
    gpu_power: float | None = None
    fan_rpm: int | None = None
    process: str | None = None

    def to_wire(self) -> dict:
        wire: dict = {
            "ts": dt.datetime.fromtimestamp(self.ts_ms / 1000, tz=dt.UTC)
            .isoformat(timespec="milliseconds")
        }
        for field in ("cpu_temp", "gpu_temp", "cpu_power", "gpu_power", "fan_rpm", "process"):
            value = getattr(self, field)
            if value is not None:  # отсутствующие поля не шлём — экономия трафика
                wire[field] = value
        return wire


def build_batch_payload(samples: list[Sample], batch_id: str) -> dict:
    if not 1 <= len(samples) <= MAX_BATCH_SAMPLES:
        raise ValueError(f"батч должен содержать 1..{MAX_BATCH_SAMPLES} сэмплов")
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "sent_at": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
        "agent_version": AGENT_VERSION,
        "samples": [s.to_wire() for s in samples],
    }


def encode_batch(payload: dict) -> bytes:
    """JSON + gzip — ровно те байты, что уходят в POST /v1/telemetry."""
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
