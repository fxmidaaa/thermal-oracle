"""Оценка T_ambient по idle-эпизодам (architecture.md §5.2).

Прямого датчика комнатной температуры нет — прокси: температура кристалла в
глубоком простое. Защита от thermal soak-back: первые 10 минут каждого эпизода
отбрасываются полностью (радиатор ещё тёплый после нагрузки и завышает оценку),
по остатку берётся низкий перцентиль (фоновая активность смещает только вверх).

Семантика порогов (важное уточнение к ТЗ): непрерывность эпизода определяет
СКОЛЬЗЯЩЕЕ СРЕДНЕЕ 30с < 5 Вт; мгновенный потолок 8 Вт не рвёт эпизод, а лишь
исключает сэмплы из оценки температуры. Иначе штатные 2-секундные всплески
фоновых задач (индексатор и т.п.) рассыпали бы любой реальный простой в пыль.

Систематика документирована: T_idle_die ≈ T_room + 2–4 °C. Для ТРЕНДА
деградации постоянное смещение безвредно, а сезонный ход комнатной температуры
этим механизмом как раз вычитается.
"""
from dataclasses import dataclass

import numpy as np

from app.analytics.params import AnalysisParams
from app.analytics.series import median_filter, rolling_mean, runs, span_s, split_on_gaps


@dataclass(slots=True)
class IdleEpisode:
    i0: int
    i1: int          # полуинтервал [i0, i1)
    duration_s: float
    estimate: float | None  # None — хвост слишком короткий/пустой


@dataclass(slots=True)
class DayAmbient:
    t_ambient: float
    confidence: float       # 0..1
    idle_minutes: int
    episodes_n: int


def find_idle_episodes(
    ts: np.ndarray, power: np.ndarray, temp: np.ndarray, params: AnalysisParams
) -> list[IdleEpisode]:
    episodes: list[IdleEpisode] = []
    for s0, s1 in split_on_gaps(ts, params.idle_gap_split_s):
        if span_s(ts, s0, s1) < params.idle_min_duration_s:
            continue
        smooth = rolling_mean(power[s0:s1], params.idle_rolling_s)
        idle_mask = np.where(np.isnan(smooth), False, smooth < params.idle_power_w)
        for r0, r1 in runs(idle_mask):
            i0, i1 = s0 + r0, s0 + r1
            duration = span_s(ts, i0, i1)
            if duration < params.idle_min_duration_s:
                continue
            episodes.append(
                IdleEpisode(i0, i1, duration, _episode_estimate(ts, power, temp, i0, i1, params))
            )
    return episodes


def _episode_estimate(
    ts: np.ndarray, power: np.ndarray, temp: np.ndarray,
    i0: int, i1: int, params: AnalysisParams,
) -> float | None:
    """Оценка эпизода: перцентиль сглаженной температуры по хвосту после
    отброса первых idle_discard_head_s (soak-back)."""
    tail_start_ts = ts[i0] + params.idle_discard_head_s
    j0 = i0 + int(np.searchsorted(ts[i0:i1], tail_start_ts))
    if j0 >= i1 or span_s(ts, j0, i1) < params.idle_min_tail_s:
        return None
    tail_temp = median_filter(temp[j0:i1], params.medfilt_s)
    tail_power = power[j0:i1]
    # мгновенные всплески исключаем из оценки (но эпизод не рвём — см. докстринг)
    quiet = ~np.isnan(tail_temp) & ~(tail_power >= params.idle_power_max_w)
    if quiet.sum() < params.idle_min_tail_s * 0.5:
        return None
    return float(np.percentile(tail_temp[quiet], params.ambient_percentile))


def estimate_day_ambient(
    episodes: list[IdleEpisode], params: AnalysisParams
) -> DayAmbient | None:
    """Дневная оценка: взвешенная длительностью медиана оценок эпизодов.
    Confidence растёт с суммой idle-минут и падает с разбросом оценок."""
    valued = [e for e in episodes if e.estimate is not None]
    if not valued:
        return None

    estimates = np.array([e.estimate for e in valued])
    weights = np.array([e.duration_s for e in valued])
    order = np.argsort(estimates)
    cum = np.cumsum(weights[order])
    t_ambient = float(estimates[order][np.searchsorted(cum, cum[-1] / 2)])

    idle_minutes = int(sum(e.duration_s for e in valued) / 60)
    spread = float(estimates.max() - estimates.min())
    confidence = min(1.0, idle_minutes / 60) * float(np.clip(1.0 - spread / 4.0, 0.2, 1.0))

    clamped = float(np.clip(t_ambient, params.ambient_clamp_low, params.ambient_clamp_high))
    if clamped != t_ambient:  # физически неправдоподобно — оценке веры меньше
        t_ambient = clamped
        confidence *= 0.5

    return DayAmbient(t_ambient, confidence, idle_minutes, len(valued))
