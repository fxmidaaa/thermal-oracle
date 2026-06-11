"""Тренд деградации: Theil–Sen + CUSUM + прогноз + health + диагностика
«паста vs пыль» (architecture.md §5.5). Чистая математика, без IO.
"""
import math
from dataclasses import dataclass

import numpy as np

from app.analytics.params import AnalysisParams

Z_95 = 1.959964  # норм. квантиль 97.5% для CI Сена


@dataclass(slots=True)
class TrendResult:
    slope_per_day: float   # K/W в день
    ci_low: float
    ci_high: float
    intercept: float

    @property
    def significant(self) -> bool:
        """CI наклона не накрывает ноль — деградация статистически видна."""
        return self.ci_low > 0.0 or self.ci_high < 0.0


def theil_sen(x: np.ndarray, y: np.ndarray) -> TrendResult | None:
    """Робастная регрессия: медиана попарных наклонов; CI — метод Сена
    (ранги наклонов через нормальную аппроксимацию дисперсии статистики
    Кендалла). Устойчива к выбросам без предположений о распределении."""
    n = len(x)
    if n < 3 or n != len(y):
        return None
    i, j = np.triu_indices(n, k=1)
    dx = x[j] - x[i]
    nonzero = dx != 0
    slopes = np.sort((y[j] - y[i])[nonzero] / dx[nonzero])
    m = slopes.size
    if m == 0:
        return None
    slope = float(np.median(slopes))
    intercept = float(np.median(y - slope * x))

    var_s = n * (n - 1) * (2 * n + 5) / 18.0
    c = Z_95 * math.sqrt(var_s)
    lo_idx = int(np.clip(math.floor((m - c) / 2), 0, m - 1))
    hi_idx = int(np.clip(math.ceil((m + c) / 2), 0, m - 1))
    return TrendResult(slope, float(slopes[lo_idx]), float(slopes[hi_idx]), intercept)


def cusum_changepoints(values: np.ndarray, k_sigma: float, h_sigma: float) -> list[int]:
    """CUSUM по ПЕРВЫМ РАЗНОСТЯМ дневных медиан, σ — через MAD.

    Почему разности: ступенька (новая кривая кулеров, андервольт, чистка) —
    это ОДНА большая разность → CUSUM мгновенно стреляет; плавный дрейф
    деградации — много маленьких разностей ниже порога k → не накапливается.
    Так шаг отделяется от тренда, и BIOS-апдейт не маскируется под пасту.
    Возвращает индексы значений, ПОСЛЕ которых произошёл сдвиг.
    """
    if values.size < 5:
        return []
    diffs = np.diff(values)
    mad = float(np.median(np.abs(diffs - np.median(diffs))))
    sigma = 1.4826 * mad
    if sigma <= 0:
        sigma = float(np.std(diffs)) or 1e-9
    z = (diffs - float(np.median(diffs))) / sigma

    changepoints: list[int] = []
    s_pos = s_neg = 0.0
    start_pos = start_neg = 0
    for idx, zi in enumerate(z):
        s_pos = max(0.0, s_pos + zi - k_sigma)
        if s_pos == 0.0:
            start_pos = idx + 1
        s_neg = max(0.0, s_neg - zi - k_sigma)
        if s_neg == 0.0:
            start_neg = idx + 1
        if s_pos > h_sigma or s_neg > h_sigma:
            # точка сдвига = максимальная |разность| внутри экскурсии (шумовой
            # дрейф мог приоткрыть экскурсию раньше самой ступеньки);
            # +1 — переход от индекса разности к индексу первого значения
            # нового режима
            start = start_pos if s_pos > h_sigma else start_neg
            peak = start + int(np.argmax(np.abs(z[start : idx + 1])))
            changepoints.append(peak + 1)
            s_pos = s_neg = 0.0
            start_pos = start_neg = idx + 1
    return changepoints


def forecast_days_to_throttle(
    rth_current: float,
    slope_per_day: float,
    p_typical: float,
    t_ambient_typical: float,
    params: AnalysisParams,
) -> float | None:
    """Решение T_amb + Rth(d)·P_typ = T_throttle. None — «не ожидается» либо
    за честным горизонтом экстраполяции."""
    if slope_per_day <= 0 or p_typical <= 0:
        return None
    rth_limit = (params.t_throttle_c - t_ambient_typical) / p_typical
    if rth_current >= rth_limit:
        return 0.0
    days = (rth_limit - rth_current) / slope_per_day
    return days if days <= params.forecast_max_days else None


def health_score(
    degradation_pct: float | None,
    days_to_throttle: float | None,
    quality_penalty: float,
) -> int | None:
    """100 − 40·deg − 40·прогноз − 20·качество данных (architecture.md §5.5)."""
    if degradation_pct is None:
        return None
    deg_term = float(np.clip(degradation_pct / 15.0, 0.0, 1.0))
    forecast_term = 0.0
    if days_to_throttle is not None:
        forecast_term = float(np.clip((180.0 - days_to_throttle) / 180.0, 0.0, 1.0))
    score = 100.0 - 40.0 * deg_term - 40.0 * forecast_term - 20.0 * quality_penalty
    return int(np.clip(round(score), 0, 100))


def diagnose(
    rth_early: np.ndarray, rpm_early: np.ndarray,
    rth_late: np.ndarray, rpm_late: np.ndarray,
    params: AnalysisParams,
) -> str:
    """Эвристика «паста vs пыль» на окнах ОДНОЙ страты мощности (§5.5).

    Rth = Rth_jc + Rth_TIM + Rth_радиатор→воздух(RPM):
    - рост Rth, подтверждённый на максимальных оборотах (верхний квартиль RPM),
      → воздух не помогает, проблема до радиатора → 'tim_degradation';
    - медианные RPM выросли при той же мощности, а Rth прежний → охлаждение
      компенсирует оборотами → вероятна пыль в рёбрах → 'dust_suspected';
    - рост Rth без подтверждения на максимальных RPM → 'mixed' (честная
      неуверенность вместо ложной точности).
    """
    if len(rth_early) < params.diag_min_windows or len(rth_late) < params.diag_min_windows:
        return "insufficient_data"

    base = float(np.median(rth_early))
    rth_shift_pct = 100.0 * (float(np.median(rth_late)) - base) / base

    have_rpm = np.isfinite(rpm_early).sum() >= 5 and np.isfinite(rpm_late).sum() >= 5
    if not have_rpm:  # без RPM пыль от пасты не отличить
        return "tim_degradation" if rth_shift_pct >= params.diag_rth_growth_pct else "none"

    rpm_base = float(np.nanmedian(rpm_early))
    rpm_shift_pct = (
        100.0 * (float(np.nanmedian(rpm_late)) - rpm_base) / rpm_base if rpm_base > 0 else 0.0
    )

    if rth_shift_pct >= params.diag_rth_growth_pct:
        threshold = np.nanpercentile(
            np.concatenate([rpm_early, rpm_late]), params.diag_high_rpm_quantile
        )
        hi_early = rth_early[rpm_early >= threshold]
        hi_late = rth_late[rpm_late >= threshold]
        if len(hi_early) >= 5 and len(hi_late) >= 5:
            hi_shift = 100.0 * (np.median(hi_late) - np.median(hi_early)) / np.median(hi_early)
            if hi_shift >= 0.6 * rth_shift_pct:
                return "tim_degradation"
        return "mixed"

    if rpm_shift_pct >= params.diag_rpm_shift_pct and rth_shift_pct < params.diag_rth_flat_pct:
        return "dust_suspected"
    return "none"
