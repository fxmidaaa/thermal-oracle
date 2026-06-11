"""Тренд: Theil–Sen с выбросами, CUSUM «ступенька vs дрейф», прогноз,
health score, диагностика «паста vs пыль»."""
import numpy as np

from app.analytics.params import AnalysisParams
from app.analytics.trend import (
    cusum_changepoints,
    diagnose,
    forecast_days_to_throttle,
    health_score,
    theil_sen,
)

P = AnalysisParams()
RNG = np.random.default_rng(42)


# ------------------------------------------------------------- Theil–Sen --

def test_theil_sen_robust_to_outliers():
    x = np.arange(60, dtype=float)
    y = 1.0 + 0.002 * x + RNG.normal(0, 0.005, 60)
    y[[5, 17, 23, 40, 51]] += 0.5                  # 8% грубых выбросов
    trend = theil_sen(x, y)
    assert abs(trend.slope_per_day - 0.002) < 0.0006   # OLS бы уехал
    assert trend.ci_low < 0.002 < trend.ci_high
    assert trend.significant


def test_theil_sen_flat_series_not_significant():
    x = np.arange(30, dtype=float)
    y = 1.0 + RNG.normal(0, 0.01, 30)
    trend = theil_sen(x, y)
    assert not trend.significant                   # CI накрывает ноль


def test_theil_sen_degenerate_input():
    assert theil_sen(np.array([1.0]), np.array([2.0])) is None


# ------------------------------------------------------------------ CUSUM --

def test_cusum_detects_step():
    """Ступенька (BIOS/кривая кулеров) ловится около места сдвига."""
    y = np.concatenate([
        1.00 + RNG.normal(0, 0.01, 50),
        1.10 + RNG.normal(0, 0.01, 40),
    ])
    points = cusum_changepoints(y, P.cusum_k_sigma, P.cusum_h_sigma)
    assert len(points) >= 1
    assert abs(points[0] - 50) <= 3


def test_cusum_ignores_gradual_drift():
    """Плавный дрейф деградации — НЕ ступенька: CUSUM по разностям молчит,
    дрейф остаётся Theil–Sen'у."""
    y = 1.0 + 0.002 * np.arange(90) + RNG.normal(0, 0.01, 90)
    assert cusum_changepoints(y, P.cusum_k_sigma, P.cusum_h_sigma) == []


def test_cusum_short_series_silent():
    assert cusum_changepoints(np.array([1.0, 1.1, 1.0]), 0.5, 5.0) == []


# ---------------------------------------------------------------- прогноз --

def test_forecast_basic():
    # rth_limit = (95-25)/60 = 1.1667; (1.1667-1.0)/0.001 ≈ 167 дней
    days = forecast_days_to_throttle(1.0, 0.001, 60.0, 25.0, P)
    assert days is not None and abs(days - 166.7) < 1.0


def test_forecast_beyond_horizon_is_none():
    assert forecast_days_to_throttle(1.0, 0.0005, 60.0, 25.0, P) is None  # ≈333д > 180


def test_forecast_negative_slope_is_none():
    assert forecast_days_to_throttle(1.0, -0.001, 60.0, 25.0, P) is None


def test_forecast_already_at_limit_is_zero():
    assert forecast_days_to_throttle(1.2, 0.001, 60.0, 25.0, P) == 0.0


# ------------------------------------------------------------ health score --

def test_health_score_healthy():
    assert health_score(0.0, None, 0.0) == 100


def test_health_score_degraded_with_forecast():
    # 100 − 40·(15/15) − 40·(150/180) − 0 ≈ 27
    assert health_score(15.0, 30.0, 0.0) == 27


def test_health_score_none_without_baseline():
    assert health_score(None, None, 0.0) is None


# ------------------------------------------------- диагностика паста/пыль --

def _rpm(n, lo=3000, hi=5000):
    return RNG.uniform(lo, hi, n)


def test_diagnose_tim_degradation():
    """Rth вырос и на максимальных оборотах тоже — воздух не помогает."""
    n = 30
    verdict = diagnose(
        rth_early=1.00 + RNG.normal(0, 0.01, n), rpm_early=_rpm(n),
        rth_late=1.15 + RNG.normal(0, 0.01, n), rpm_late=_rpm(n),
        params=P,
    )
    assert verdict == "tim_degradation"


def test_diagnose_dust_suspected():
    """Rth прежний, но даётся ценой выросших оборотов → пыль в рёбрах."""
    n = 30
    verdict = diagnose(
        rth_early=1.00 + RNG.normal(0, 0.01, n), rpm_early=_rpm(n, 3300, 3700),
        rth_late=1.02 + RNG.normal(0, 0.01, n), rpm_late=_rpm(n, 3900, 4300),
        params=P,
    )
    assert verdict == "dust_suspected"


def test_diagnose_healthy():
    n = 30
    verdict = diagnose(
        rth_early=1.0 + RNG.normal(0, 0.01, n), rpm_early=_rpm(n),
        rth_late=1.0 + RNG.normal(0, 0.01, n), rpm_late=_rpm(n),
        params=P,
    )
    assert verdict == "none"


def test_diagnose_mixed_when_high_rpm_not_confirming():
    """Rth вырос, но на максимальных RPM — нет: честное 'mixed'."""
    n = 40
    rpm_early, rpm_late = _rpm(n), _rpm(n)
    threshold = np.percentile(np.concatenate([rpm_early, rpm_late]), 75)
    rth_late = np.where(rpm_late >= threshold,
                        1.00 + RNG.normal(0, 0.005, n),
                        1.20 + RNG.normal(0, 0.005, n))
    verdict = diagnose(
        rth_early=1.0 + RNG.normal(0, 0.005, n), rpm_early=rpm_early,
        rth_late=rth_late, rpm_late=rpm_late,
        params=P,
    )
    assert verdict == "mixed"


def test_diagnose_insufficient_data():
    verdict = diagnose(np.ones(5), _rpm(5), np.ones(5), _rpm(5), P)
    assert verdict == "insufficient_data"


def test_diagnose_without_rpm_falls_back_to_rth_only():
    n = 30
    nan_rpm = np.full(n, np.nan)
    grown = diagnose(np.full(n, 1.0), nan_rpm, np.full(n, 1.15), nan_rpm, P)
    flat = diagnose(np.full(n, 1.0), nan_rpm, np.full(n, 1.01), nan_rpm, P)
    assert grown == "tim_degradation"
    assert flat == "none"
