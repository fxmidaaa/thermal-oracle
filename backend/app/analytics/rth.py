"""Rth-точка из стационарного окна (architecture.md §5.4).

Rth = (T_tail − T_ambient) / P_tail [K/W] — хвост окна как самая
установившаяся часть. Страта по мощности хвоста: сопротивление зависит от
рабочей точки, сравнивать можно только подобное с подобным.
"""
from dataclasses import dataclass

import numpy as np

from app.analytics.params import AnalysisParams
from app.analytics.series import geometric_mean
from app.analytics.windows import WindowStats


def stratum_of(p_tail: float) -> str:
    if p_tail < 50.0:
        return "p35_50"
    if p_tail < 80.0:
        return "p50_80"
    return "p80plus"


@dataclass(slots=True)
class RthPoint:
    window: WindowStats
    rth: float
    stratum: str
    t_ambient: float
    ambient_confidence: float
    quality: float  # качество окна × уверенность в ambient


def attach_rth(
    windows: list[WindowStats],
    t_ambient: float,
    ambient_confidence: float,
    params: AnalysisParams,
) -> list[RthPoint]:
    points = []
    for w in windows:
        delta = w.t_tail - t_ambient
        if delta <= 0:  # кристалл «холоднее комнаты» — мусорная точка
            continue
        # Вклад ambient в качество смягчён до [0.5..1]: ошибка ambient — общий
        # сдвиг УРОВНЯ для всех окон дня, дневные медианы и наклон тренда она
        # почти не искажает. Полное перемножение наказывало дважды: при
        # confidence 0.10 (короткие шумные эпизоды i9-13900HX) хорошие окна
        # падали до quality ~0.3 и навсегда выбывали из тренда (гейт 0.5).
        ambient_factor = 0.5 + 0.5 * float(np.clip(ambient_confidence, 0.0, 1.0))
        points.append(
            RthPoint(
                window=w,
                rth=delta / w.p_tail,
                stratum=stratum_of(w.p_tail),
                t_ambient=t_ambient,
                ambient_confidence=ambient_confidence,
                quality=geometric_mean([w.quality, ambient_factor]),
            )
        )
    return points
