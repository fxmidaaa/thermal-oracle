"""Правила выбора сенсоров на реалистичном дереве LHM (AMD CPU + iGPU + dGPU
NVIDIA + материнка с EC): локализованные значения, ловушки, приоритеты."""
import json
from pathlib import Path

import pytest

from thermal_agent.collectors.windows_lhm import extract, parse_value

TREE = json.loads((Path(__file__).parent / "data" / "lhm_sample.json").read_text("utf-8"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("45,5 °C", 45.5),          # десятичная запятая (ru-RU локаль)
        ("45.5 °C", 45.5),          # и точка тоже
        ("3 540 RPM", 3540.0),      # пробельный разделитель тысяч
        ("0 RPM", 0.0),
        ("-12,5 °C", -12.5),
        ("85,7 W", 85.7),
        ("-", None),                # сенсор молчит
        ("", None),
        (None, None),
        (42, None),                 # не строка — защита от сюрпризов формата
    ],
)
def test_parse_value(raw, expected):
    assert parse_value(raw) == expected


def test_cpu_max_temp_excludes_distance_to_tjmax():
    reading = extract(TREE)
    # max(62.0, 60.5); 33.0 «Distance to TjMax» исключён — это обратная величина
    assert reading.cpu_temp == 62.0
    assert "Core (Tctl/Tdie)" in reading.details["cpu_temp"]


def test_cpu_power_prefers_package():
    reading = extract(TREE)
    assert reading.cpu_power == 45.2  # CPU Package, не CPU Cores
    assert "Package" in reading.details["cpu_power"]


def test_discrete_gpu_preferred_over_igpu():
    reading = extract(TREE)
    assert reading.details["gpu_node"] == "NVIDIA GeForce RTX 3070 Laptop GPU"
    assert reading.gpu_temp == 64.0   # max включает Hot Spot
    assert reading.gpu_power == 85.7


def test_fan_rpm_is_max_across_all_fans():
    reading = extract(TREE)
    assert reading.fan_rpm == 3540    # EC-вентилятор громче GPU (2100)
    # Controls (61 %) не попал в вентиляторы, молчащий Fan #3 не сломал парсер
    assert len(reading.details["fans_found"]) == 3


def test_empty_tree_yields_empty_reading():
    reading = extract({"id": 0, "Text": "Sensor", "Children": []})
    assert reading.cpu_temp is None
    assert reading.gpu_temp is None
    assert reading.fan_rpm is None
