"""Санитизация сэмплов: физические диапазоны и временные ворота (без БД)."""
import datetime as dt

from app.schemas.telemetry import TelemetrySample
from app.services.ingest_service import (
    FLAG_OUT_OF_RANGE,
    FUTURE_TOLERANCE,
    LATE_HORIZON,
    sanitize_sample,
)

NOW = dt.datetime.now(dt.UTC)


def sample(ts=NOW, **fields) -> TelemetrySample:
    return TelemetrySample(ts=ts, **fields)


def test_normal_sample_untouched():
    values, flags = sanitize_sample(sample(cpu_temp=85.0, cpu_power=60.0, fan_rpm=4000), NOW)
    assert values == {
        "cpu_temp": 85.0, "gpu_temp": None,
        "cpu_power": 60.0, "gpu_power": None, "fan_rpm": 4000,
    }
    assert flags == 0


def test_out_of_range_nulled_and_flagged():
    values, flags = sanitize_sample(sample(cpu_temp=500.0, cpu_power=60.0), NOW)
    assert values["cpu_temp"] is None          # глюк сенсора обнулён...
    assert values["cpu_power"] == 60.0         # ...остальные поля целы
    assert flags & FLAG_OUT_OF_RANGE


def test_negative_power_nulled():
    values, flags = sanitize_sample(sample(cpu_power=-3.0), NOW)
    assert values["cpu_power"] is None
    assert flags & FLAG_OUT_OF_RANGE


def test_boundary_values_kept():
    values, flags = sanitize_sample(sample(cpu_temp=120.0, cpu_power=0.0), NOW)
    assert values["cpu_temp"] == 120.0
    assert values["cpu_power"] == 0.0
    assert flags == 0


def test_future_sample_rejected():
    values, _ = sanitize_sample(sample(ts=NOW + FUTURE_TOLERANCE + dt.timedelta(seconds=1)), NOW)
    assert values is None


def test_slightly_future_sample_kept():
    """Небольшой clock skew — это норма, не отбрасываем."""
    values, _ = sanitize_sample(sample(ts=NOW + dt.timedelta(seconds=30), cpu_temp=50.0), NOW)
    assert values is not None


def test_stale_sample_rejected():
    values, _ = sanitize_sample(sample(ts=NOW - LATE_HORIZON - dt.timedelta(hours=1)), NOW)
    assert values is None


def test_late_but_within_horizon_kept():
    """Дренаж оффлайн-спула: данные 3-дневной давности принимаются."""
    values, _ = sanitize_sample(sample(ts=NOW - dt.timedelta(days=3), cpu_temp=50.0), NOW)
    assert values is not None
