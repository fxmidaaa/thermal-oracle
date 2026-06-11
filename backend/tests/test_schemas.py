"""Структурная валидация wire-контракта (без БД)."""
import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.telemetry import MAX_BATCH_SAMPLES, TelemetryBatch, TelemetrySample

NOW = dt.datetime.now(dt.UTC)


def make_batch(**overrides) -> dict:
    batch = {
        "schema_version": 1,
        "batch_id": str(uuid.uuid4()),
        "sent_at": NOW.isoformat(),
        "agent_version": "test",
        "samples": [{"ts": NOW.isoformat(), "cpu_temp": 50.0, "cpu_power": 10.0}],
    }
    batch.update(overrides)
    return batch


def test_valid_batch_parses():
    batch = TelemetryBatch.model_validate(make_batch())
    assert batch.samples[0].cpu_temp == 50.0
    assert batch.samples[0].gpu_temp is None  # отсутствующий сенсор — None


def test_naive_timestamp_rejected():
    naive = {"ts": "2026-06-11T10:00:00", "cpu_temp": 50.0}
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate(make_batch(samples=[naive]))


def test_unknown_schema_version_rejected():
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate(make_batch(schema_version=2))


def test_oversized_batch_rejected():
    sample = {"ts": NOW.isoformat(), "cpu_temp": 50.0}
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate(make_batch(samples=[sample] * (MAX_BATCH_SAMPLES + 1)))


def test_empty_batch_rejected():
    with pytest.raises(ValidationError):
        TelemetryBatch.model_validate(make_batch(samples=[]))


def test_extra_field_rejected():
    """Новые поля = bump schema_version, молча игнорировать нельзя."""
    sample = {"ts": NOW.isoformat(), "cpu_temp": 50.0, "npu_power": 5.0}
    with pytest.raises(ValidationError):
        TelemetrySample.model_validate(sample)


def test_out_of_phys_range_passes_structural_validation():
    """Физические диапазоны — забота ingest_service, не схемы (поле→NULL+флаг)."""
    sample = TelemetrySample.model_validate({"ts": NOW.isoformat(), "cpu_temp": 500.0})
    assert sample.cpu_temp == 500.0
