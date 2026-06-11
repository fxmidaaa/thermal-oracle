"""Детектор стационарных окон нагрузки (architecture.md §5.3).

Физика: Rth = ΔT/P валидно только в квазистационаре (τ связки кристалл-радиатор
~5–20 с), поэтому окна — это «P > 35 Вт непрерывно ≥ 15 с», с гистерезисом
35/30 Вт против дребезга и грейсом 3 с на провалы. Поверх длительности — два
гейта качества: CV(P) < 0.2 (отсев «пилы», формально проходящей пороги) и
|dT/dt| хвоста < 0.15 °C/с (фактический выход на тепловое плато).

Реализация векторная: state machine выражена через маски/run-length поверх
NumPy (bridge провалов ≤ 3 с между True-ранами), а не через цикл по сэмплам.
NaN мощности трактуется как «ниже порога» (молчащий сенсор = провал, который
закроет грейс либо порвёт окно — честнее, чем интерполяция нагрева).
"""
from collections import Counter
from dataclasses import dataclass

import numpy as np

from app.analytics.params import AnalysisParams
from app.analytics.series import geometric_mean, median_filter, runs, span_s, split_on_gaps


@dataclass(slots=True)
class WindowStats:
    start_ts: float
    end_ts: float
    duration_s: float
    n: int
    p_mean: float
    p_cv: float
    p_tail: float
    t_tail: float
    dtdt_tail: float
    rpm_avg: float | None
    quality: float  # композит без ambient-компоненты (она домножается в rth.py)


def detect_stable_windows(
    ts: np.ndarray,
    power: np.ndarray,
    temp: np.ndarray,
    rpm: np.ndarray,
    params: AnalysisParams,
) -> tuple[list[WindowStats], Counter]:
    """→ (прошедшие все гейты окна, счётчик причин отбраковки)."""
    rejected: Counter = Counter()
    out: list[WindowStats] = []
    if ts.size == 0:
        return out, rejected
    temp_f = median_filter(temp, params.medfilt_s)

    for s0, s1 in split_on_gaps(ts, params.gap_split_s):
        stay = np.where(np.isnan(power[s0:s1]), False, power[s0:s1] >= params.load_exit_w)
        stay = _bridge_short_dips(stay, ts[s0:s1], params.dip_grace_s)
        for r0, r1 in runs(stay):
            seg_power = power[s0 + r0 : s0 + r1]
            above_enter = np.flatnonzero(seg_power > params.load_enter_w)
            if above_enter.size == 0:
                continue  # нагрузка так и не пересекла порог входа
            w0 = s0 + r0 + int(above_enter[0])  # окно открывается на P > 35 Вт
            w1 = s0 + r1
            for c0, c1 in _chunks(ts, w0, w1, params.window_max_s):
                stats = _window_stats(ts, power, temp_f, rpm, c0, c1, params, rejected)
                if stats is not None:
                    out.append(stats)
    return out, rejected


def _bridge_short_dips(stay: np.ndarray, ts: np.ndarray, grace_s: float) -> np.ndarray:
    """Провалы (False) длительностью ≤ grace_s СТРОГО МЕЖДУ True-ранами → True.
    Провалы на краях сегмента не мостим: окно не должно начинаться/кончаться дипом."""
    bridged = stay.copy()
    for f0, f1 in runs(~stay):
        if f0 == 0 or f1 == stay.size:
            continue
        if span_s(ts, f0, f1) <= grace_s:
            bridged[f0:f1] = True
    return bridged


def _chunks(ts: np.ndarray, w0: int, w1: int, max_s: float) -> list[tuple[int, int]]:
    """Сессии длиннее max_s режем по времени (гранулярные Rth-точки)."""
    chunks = []
    c0 = w0
    while c0 < w1:
        # side="left": сэмпл ровно на границе max_s уходит в следующий чанк,
        # иначе длительность чанка получалась бы max_s + 1с
        cutoff = ts[c0] + max_s
        c1 = c0 + int(np.searchsorted(ts[c0:w1], cutoff, side="left"))
        chunks.append((c0, c1))
        c0 = c1
    return chunks


def _window_stats(
    ts: np.ndarray,
    power: np.ndarray,
    temp_f: np.ndarray,
    rpm: np.ndarray,
    c0: int,
    c1: int,
    params: AnalysisParams,
    rejected: Counter,
) -> WindowStats | None:
    duration = span_s(ts, c0, c1)
    if duration < params.window_min_s:
        rejected["too_short"] += 1
        return None
    n = c1 - c0
    if n < params.completeness_min * duration:
        rejected["incomplete"] += 1
        return None

    p_win = power[c0:c1]
    p_mean = float(np.nanmean(p_win))
    if not np.isfinite(p_mean) or p_mean <= 0:
        rejected["no_power"] += 1
        return None
    p_cv = float(np.nanstd(p_win) / p_mean)
    if p_cv >= params.cv_max:
        rejected["unstable_power"] += 1  # «пила» — гейт CV(P)
        return None

    tail_s = min(params.tail_s, duration / 3)
    t0 = c0 + int(np.searchsorted(ts[c0:c1], ts[c1 - 1] - tail_s))
    tail_temp = temp_f[t0:c1]
    tail_ts = ts[t0:c1]
    valid = np.isfinite(tail_temp)
    if valid.sum() < 3:
        rejected["no_temp"] += 1
        return None
    dtdt = float(np.polyfit(tail_ts[valid] - tail_ts[0], tail_temp[valid], 1)[0])
    if abs(dtdt) >= params.tail_dtdt_max:
        rejected["not_settled"] += 1  # температура ещё не вышла на плато
        return None

    t_tail = float(np.nanmean(tail_temp))
    p_tail = float(np.nanmean(power[t0:c1]))
    rpm_win = rpm[c0:c1]
    rpm_avg = float(np.nanmean(rpm_win)) if np.isfinite(rpm_win).any() else None

    quality = geometric_mean([
        min(1.0, duration / 60.0),                 # длиннее окно — надёжнее точка
        1.0 - p_cv / params.cv_max,                # стабильнее мощность — лучше
        1.0 - abs(dtdt) / params.tail_dtdt_max,    # глубже плато — лучше
        n / duration,                              # полнота сэмплов
    ])
    return WindowStats(
        start_ts=float(ts[c0]), end_ts=float(ts[c1 - 1]), duration_s=duration, n=n,
        p_mean=p_mean, p_cv=p_cv, p_tail=p_tail,
        t_tail=t_tail, dtdt_tail=dtdt, rpm_avg=rpm_avg, quality=quality,
    )
