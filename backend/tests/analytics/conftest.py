"""Генератор синтетических 1 Гц рядов с тепловой физикой первого порядка:
температура релаксирует к целевой экспоненциально с постоянной τ —
ровно та модель, против которой спроектированы гейты детектора.
"""
import numpy as np
import pytest


def build_series(
    segments: list[tuple[float, float, float]],
    tau_s: float = 8.0,
    noise_p: float = 0.0,
    noise_t: float = 0.0,
    t0: float = 1_780_000_000.0,
    start_temp: float | None = None,
    seed: int = 7,
):
    """segments: [(длительность_с, мощность_Вт, целевая_температура_°C)].
    → (ts, power, temp) как float-массивы 1 Гц."""
    rng = np.random.default_rng(seed)
    ts, power, temp = [], [], []
    current = start_temp if start_temp is not None else segments[0][2]
    t = t0
    decay = None
    for duration, p_w, t_target in segments:
        for _ in range(int(duration)):
            if decay is None or decay[0] != tau_s:
                decay = (tau_s, 1.0 - np.exp(-1.0 / tau_s))
            current += (t_target - current) * decay[1]
            ts.append(t)
            power.append(p_w + (noise_p * rng.standard_normal() if noise_p else 0.0))
            temp.append(current + (noise_t * rng.standard_normal() if noise_t else 0.0))
            t += 1.0
    return (
        np.array(ts, dtype=float),
        np.array(power, dtype=float),
        np.array(temp, dtype=float),
    )


@pytest.fixture
def no_rpm():
    """Массив RPM «нет данных» нужной длины."""
    return lambda n: np.full(n, np.nan)
