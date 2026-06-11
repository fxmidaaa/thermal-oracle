"""Contract-тесты agent ↔ backend (architecture.md §7).

Агент НАМЕРЕННО дублирует wire-схемы (не зависит от пакета backend) — эти
тесты единственное, что держит две копии контракта в синхроне. Запуск требует
обоих пакетов в одном окружении: pip install -e ../agent
"""
import gzip
import json

import pytest
from pydantic import ValidationError

pytest.importorskip(
    "thermal_agent",
    reason="агент не установлен: pip install -e ../agent",
)

from thermal_agent import models as agent_models  # noqa: E402
from thermal_agent.models import Sample, build_batch_payload, encode_batch, uuid7  # noqa: E402

from app.schemas.telemetry import MAX_BATCH_SAMPLES, TelemetryBatch  # noqa: E402


def make_samples(n: int) -> list[Sample]:
    base_ms = 1_780_000_000_000
    samples = []
    for i in range(n):
        if i % 3 == 0:    # полный сэмпл
            s = Sample(base_ms + i * 1000, cpu_temp=85.0, gpu_temp=70.5,
                       cpu_power=62.3, gpu_power=15.0, fan_rpm=4300, process="game.exe")
        elif i % 3 == 1:  # частично молчащие сенсоры
            s = Sample(base_ms + i * 1000, cpu_temp=84.0, cpu_power=60.0)
        else:             # только метка времени (все сенсоры молчат)
            s = Sample(base_ms + i * 1000)
        samples.append(s)
    return samples


def test_agent_payload_validates_against_backend_schema():
    payload = build_batch_payload(make_samples(30), str(uuid7()))
    batch = TelemetryBatch.model_validate(payload)   # extra="forbid" — строгая проверка
    assert len(batch.samples) == 30
    assert batch.samples[0].ts.tzinfo is not None    # tz-aware дошёл
    assert batch.samples[0].process == "game.exe"
    assert batch.samples[2].cpu_temp is None         # молчащий сенсор = None


def test_gzip_bytes_decode_to_same_payload():
    """encode_batch — ровно те байты, что распакует бэкенд-middleware."""
    payload = build_batch_payload(make_samples(10), str(uuid7()))
    body = encode_batch(payload)
    assert json.loads(gzip.decompress(body)) == payload
    assert len(body) < 1 * 2**20                     # лимит MAX_COMPRESSED middleware


def test_max_batch_constants_in_sync():
    assert agent_models.MAX_BATCH_SAMPLES == MAX_BATCH_SAMPLES


def test_backend_rejects_oversized_batch_and_agent_never_builds_one():
    samples = make_samples(MAX_BATCH_SAMPLES + 1)
    with pytest.raises(ValueError):                  # агент не соберёт...
        build_batch_payload(samples, str(uuid7()))
    payload = build_batch_payload(samples[:MAX_BATCH_SAMPLES], str(uuid7()))
    payload["samples"].append(payload["samples"][-1] | {"ts": "2026-06-11T00:00:00+00:00"})
    with pytest.raises(ValidationError):             # ...а бэкенд не примет
        TelemetryBatch.model_validate(payload)


def test_uuid7_is_valid_uuid_version_7():
    value = uuid7()
    assert value.version == 7


def test_agent_pair_payload_validates_against_backend_schema():
    from thermal_agent.pairing import build_pair_payload  # noqa: PLC0415

    from app.schemas.devices import PairRequest  # noqa: PLC0415

    payload = build_pair_payload("AB23-CD45", "Test Legion", "windows")
    parsed = PairRequest.model_validate(payload)
    assert parsed.code == "AB23-CD45"
    assert parsed.platform == "windows"


def test_full_batch_boundary_accepted():
    payload = build_batch_payload(make_samples(MAX_BATCH_SAMPLES), str(uuid7()))
    assert len(TelemetryBatch.model_validate(payload).samples) == MAX_BATCH_SAMPLES
