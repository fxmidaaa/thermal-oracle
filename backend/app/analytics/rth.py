"""Rth-точка из стационарного окна (architecture.md §5.4).

Rth = (T_tail − T_ambient) / P_tail [K/W] — хвост окна как самая
установившаяся часть. Страта по мощности хвоста: сопротивление зависит от
рабочей точки, сравнивать можно только подобное с подобным.
"""
from dataclasses import dataclass

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
        points.append(
            RthPoint(
                window=w,
                rth=delta / w.p_tail,
                stratum=stratum_of(w.p_tail),
                t_ambient=t_ambient,
                ambient_confidence=ambient_confidence,
                quality=geometric_mean([w.quality, ambient_confidence]),
            )
        )
    return points
