"""Интерфейс коллектора. v1 — только windows_lhm; macos_powermetrics в v2
реализует тот же протокол (ADR-0001)."""
from typing import Protocol

from thermal_agent.models import Sample


class Collector(Protocol):
    def sample(self) -> Sample | None:
        """Снять показания. None — источник целиком недоступен (разрыв ряда
        честнее фиктивных значений). Частично недоступные сенсоры — None
        в соответствующих полях Sample."""
        ...

    def detect(self) -> dict:
        """Отчёт: какие сенсоры найдены и что выбрали правила (для CLI)."""
        ...

    def close(self) -> None: ...
