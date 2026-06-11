"""Коллектор Windows: парсер JSON-дерева LibreHardwareMonitor.

LHM работает с админ-правами и отдаёт сенсоры через встроенный веб-сервер
(Options → Remote Web Server → Run, порт 8085, /data.json). Агент остаётся
непривилегированным и раз в секунду опрашивает localhost — ровно та схема
обхода Ring-0 ограничений, что зафиксирована в architecture.md §2.1.

Автодетекция вместо хардкода имён: дерево различается между вендорами и
версиями LHM, а числовые id узлов меняются между перезапусками LHM. Поэтому
на КАЖДОМ опросе свежее дерево прогоняется через детерминированные ПРАВИЛА
выбора (по ImageURL железа, Text категорий и имён сенсоров) — самоисцеление
после рестарта LHM бесплатно, а `detect-sensors` — просто dry-run тех же
правил. Значения локализованы («45,5 °C», «3 540 RPM») — парсер обязан есть
запятые и пробельные разделители тысяч.

Правила выбора (см. тесты tests/test_lhm_parser.py):
- CPU: узел с иконкой cpu.png; температура = MAX по всем сенсорам категории
  Temperatures, исключая «Distance to TjMax» (это обратная величина);
  мощность = сенсор Powers с «package» в имени, иначе «cpu»/«power», иначе max.
- GPU: узлы с иконками nvidia.png/ati.png или GPU-именами; при нескольких
  (iGPU+dGPU) предпочитаем дискретную (GeForce/RTX/GTX/Radeon RX/Arc), затем
  ту, у которой есть Powers. Температура = MAX (включая Hot Spot — для тренда
  важна стабильность выбора, не абсолютная величина), мощность — как у CPU.
- Вентиляторы: MAX по ВСЕМ сенсорам категорий Fans во всём дереве
  (материнка/EC/GPU) — architecture.md §2.1.
"""
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from thermal_agent.models import Sample
from thermal_agent.win_foreground import get_foreground_process

log = logging.getLogger(__name__)

_NUM_RE = re.compile(r"^\s*(-?\d[\d\s ]*(?:[.,]\d+)?)")
_GPU_NAME_RE = re.compile(r"GeForce|RTX|GTX|Radeon|\bArc\b|Iris|Graphics", re.IGNORECASE)
_DISCRETE_GPU_RE = re.compile(r"GeForce|RTX|GTX|Radeon\s+RX|\bArc\b", re.IGNORECASE)
_WARN_THROTTLE_S = 60.0


def parse_value(text: object) -> float | None:
    """'45,5 °C' → 45.5; '3 540 RPM' → 3540.0; '-' / '' / None → None."""
    if not isinstance(text, str):
        return None
    match = _NUM_RE.match(text)
    if match is None:
        return None
    number = match.group(1).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


@dataclass
class Reading:
    cpu_temp: float | None = None
    cpu_power: float | None = None
    gpu_temp: float | None = None
    gpu_power: float | None = None
    fan_rpm: int | None = None
    # человекочитаемые пути выбранных сенсоров — для detect-sensors и отладки
    details: dict[str, object] = field(default_factory=dict)


def _walk(node: dict, path: tuple[str, ...]):
    yield node, path
    for child in node.get("Children") or []:
        yield from _walk(child, path + (child.get("Text", ""),))


def _categories(hw_node: dict) -> dict[str, dict]:
    return {ch.get("Text", ""): ch for ch in hw_node.get("Children") or []}


def _sensors(category: dict | None) -> list[tuple[str, float | None]]:
    if category is None:
        return []
    return [
        (ch.get("Text", ""), parse_value(ch.get("Value")))
        for ch in category.get("Children") or []
        if "Value" in ch
    ]


def _classify_hardware(tree: dict) -> tuple[list[dict], list[dict]]:
    """→ (cpu-узлы, gpu-узлы) по иконкам/именам."""
    cpus, gpus = [], []
    for node, _path in _walk(tree, ()):
        icon = (node.get("ImageURL") or "").lower()
        text = node.get("Text", "")
        if icon.endswith("cpu.png"):
            cpus.append(node)
        elif icon.endswith(("nvidia.png", "ati.png")):
            gpus.append(node)
        elif _GPU_NAME_RE.search(text) and (
            "Temperatures" in _categories(node) or "Powers" in _categories(node)
        ):
            gpus.append(node)  # fallback по имени (Intel iGPU/Arc без знакомой иконки)
    return cpus, gpus


def _pick_power(sensors: list[tuple[str, float | None]]) -> tuple[str, float] | None:
    valued = [(n, v) for n, v in sensors if v is not None]
    if not valued:
        return None
    for needle in ("package", "cpu", "gpu", "power"):
        for name, value in valued:
            if needle in name.lower():
                return name, value
    return max(valued, key=lambda nv: nv[1])


def _max_temp(
    sensors: list[tuple[str, float | None]], exclude: str = "distance"
) -> tuple[str, float] | None:
    valued = [(n, v) for n, v in sensors if v is not None and exclude not in n.lower()]
    if not valued:
        return None
    return max(valued, key=lambda nv: nv[1])


def extract(tree: dict) -> Reading:
    reading = Reading()
    cpus, gpus = _classify_hardware(tree)

    if cpus:
        cpu = cpus[0]
        cats = _categories(cpu)
        temp = _max_temp(_sensors(cats.get("Temperatures")))
        power = _pick_power(_sensors(cats.get("Powers")))
        if temp:
            reading.cpu_temp = temp[1]
            reading.details["cpu_temp"] = f"{cpu.get('Text')} / {temp[0]}"
        if power:
            reading.cpu_power = power[1]
            reading.details["cpu_power"] = f"{cpu.get('Text')} / {power[0]}"
        reading.details["cpu_node"] = cpu.get("Text")

    if gpus:
        def gpu_score(node: dict) -> tuple[int, int]:
            discrete = bool(_DISCRETE_GPU_RE.search(node.get("Text", "")))
            has_power = bool(_sensors(_categories(node).get("Powers")))
            return (int(discrete), int(has_power))

        gpu = max(gpus, key=gpu_score)
        cats = _categories(gpu)
        temp = _max_temp(_sensors(cats.get("Temperatures")))
        power = _pick_power(_sensors(cats.get("Powers")))
        if temp:
            reading.gpu_temp = temp[1]
            reading.details["gpu_temp"] = f"{gpu.get('Text')} / {temp[0]}"
        if power:
            reading.gpu_power = power[1]
            reading.details["gpu_power"] = f"{gpu.get('Text')} / {power[0]}"
        reading.details["gpu_node"] = gpu.get("Text")

    fans: list[tuple[str, float]] = []
    for node, path in _walk(tree, ()):
        if node.get("Text") == "Fans":
            fans.extend(
                (" / ".join(path[-2:] + (name,)), value)
                for name, value in _sensors(node)
                if value is not None
            )
    if fans:
        name, value = max(fans, key=lambda nv: nv[1])
        reading.fan_rpm = int(round(value))
        reading.details["fan_rpm"] = name
        reading.details["fans_found"] = [n for n, _ in fans]

    return reading


class LhmCollector:
    def __init__(self, url: str, timeout: float = 0.8):
        self._url = url
        self._client = httpx.Client(timeout=timeout)
        self._last_warn = 0.0

    def _fetch_tree(self) -> dict | None:
        try:
            response = self._client.get(self._url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            now = time.monotonic()
            if now - self._last_warn > _WARN_THROTTLE_S:
                self._last_warn = now
                log.warning(
                    "LibreHardwareMonitor недоступен (%s): %s — пишу разрыв ряда",
                    self._url, exc,
                )
            return None

    def sample(self) -> Sample | None:
        tree = self._fetch_tree()
        if tree is None:
            return None
        reading = extract(tree)
        return Sample(
            ts_ms=time.time_ns() // 1_000_000,
            cpu_temp=reading.cpu_temp,
            gpu_temp=reading.gpu_temp,
            cpu_power=reading.cpu_power,
            gpu_power=reading.gpu_power,
            fan_rpm=reading.fan_rpm,
            process=get_foreground_process(),
        )

    def detect(self) -> dict:
        """Для CLI: бросает httpx.HTTPError, если LHM недоступен."""
        response = self._client.get(self._url)
        response.raise_for_status()
        reading = extract(response.json())
        return {
            "reading": reading,
            "capabilities": {
                "has_cpu_temp": reading.cpu_temp is not None,
                "has_cpu_power": reading.cpu_power is not None,
                "has_gpu_temp": reading.gpu_temp is not None,
                "has_gpu_power": reading.gpu_power is not None,
                "has_fan_rpm": reading.fan_rpm is not None,
            },
        }

    def close(self) -> None:
        self._client.close()
