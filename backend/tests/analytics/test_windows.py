"""Детектор стационарных окон: гистерезис, грейс, CV-гейт, плато, гэпы."""
import numpy as np

from app.analytics.params import AnalysisParams
from app.analytics.rth import attach_rth, stratum_of
from app.analytics.windows import detect_stable_windows
from tests.analytics.conftest import build_series

P = AnalysisParams()


def detect(ts, power, temp, params=P):
    rpm = np.full(ts.size, np.nan)
    return detect_stable_windows(ts, power, temp, rpm, params)


def test_ideal_load_window():
    """Фикстура «идеальное окно»: ступенька 3→62 Вт, τ=8с, плато к хвосту."""
    ts, power, temp = build_series(
        [(30, 3.0, 45.0), (90, 62.0, 85.0)], tau_s=8.0, noise_p=0.4, noise_t=0.1
    )
    windows, rejected = detect(ts, power, temp)
    assert len(windows) == 1
    w = windows[0]
    assert abs(w.duration_s - 90) <= 2          # окно открылось на входе в нагрузку
    assert w.p_cv < 0.05
    assert abs(w.t_tail - 85.0) < 1.0           # хвост на плато
    assert abs(w.dtdt_tail) < 0.15
    assert w.quality > 0.5
    assert not rejected

    points = attach_rth(windows, t_ambient=25.0, ambient_confidence=1.0, params=P)
    assert abs(points[0].rth - (85.0 - 25.0) / 62.0) < 0.05   # ≈0.97 K/W
    assert points[0].stratum == "p50_80"


def test_noisy_saw_rejected_by_cv():
    """Фикстура «шумная пила»: 36↔80 Вт каждые 2с — гистерезис держит окно
    открытым (P не падает ниже 30), но CV(P)≈0.4 ≥ 0.2 бракует точку."""
    segments = []
    for _ in range(23):
        segments.append((2, 36.0, 70.0))
        segments.append((2, 80.0, 70.0))
    ts, power, temp = build_series(segments, tau_s=0.1)
    windows, rejected = detect(ts, power, temp)
    assert windows == []
    assert rejected["unstable_power"] >= 1


def test_short_dip_bridged_by_grace():
    """Провал до 25 Вт на 2с НЕ рвёт окно (грейс 3с)."""
    ts, power, temp = build_series(
        [(30, 60.0, 84.0), (2, 25.0, 84.0), (30, 60.0, 84.0)], tau_s=0.1
    )
    windows, _ = detect(ts, power, temp)
    assert len(windows) == 1
    assert windows[0].duration_s >= 60


def test_long_dip_splits_window():
    """Провал на 5с (> грейса) закрывает окно; обе половины ≥15с выживают."""
    ts, power, temp = build_series(
        [(30, 60.0, 84.0), (5, 25.0, 84.0), (30, 60.0, 84.0)], tau_s=0.1
    )
    windows, _ = detect(ts, power, temp)
    assert len(windows) == 2


def test_impulse_spike_below_min_duration_ignored():
    """Импульс 60 Вт на 3с из простоя — окна нет (< 15с)."""
    ts, power, temp = build_series(
        [(60, 3.0, 45.0), (3, 60.0, 50.0), (60, 3.0, 45.0)], tau_s=0.1
    )
    windows, rejected = detect(ts, power, temp)
    assert windows == []
    assert rejected["too_short"] == 1


def test_exact_min_duration_boundary():
    """Ровно 15с — принимается; 14с — нет (граница ≥)."""
    base = [(30, 3.0, 84.0)]  # старт уже на целевой температуре → плато мгновенно
    ts, p, t = build_series(base + [(15, 60.0, 84.0)], tau_s=0.1, start_temp=84.0)
    accepted, _ = detect(ts, p, t)
    assert len(accepted) == 1
    ts, p, t = build_series(base + [(14, 60.0, 84.0)], tau_s=0.1, start_temp=84.0)
    rejected_windows, rejected = detect(ts, p, t)
    assert rejected_windows == []
    assert rejected["too_short"] == 1


def test_data_gap_splits_window():
    """Пропуск сэмплов > 2с рвёт окно: нагрев не интерполируем."""
    ts, power, temp = build_series([(70, 60.0, 84.0)], tau_s=0.1)
    keep = (ts - ts[0] < 30) | (ts - ts[0] >= 36)   # вырезаем 6 секунд
    windows, _ = detect(ts[keep], power[keep], temp[keep])
    assert len(windows) == 2


def test_not_settled_tail_rejected():
    """Температура ещё растёт (τ=20с, окно 20с) → |dT/dt| хвоста ≥ 0.15 → брак."""
    ts, power, temp = build_series([(20, 60.0, 85.0)], tau_s=20.0, start_temp=45.0)
    windows, rejected = detect(ts, power, temp)
    assert windows == []
    assert rejected["not_settled"] == 1


def test_long_session_chunked():
    """Сессия 25 мин режется на чанки ≤ window_max_s."""
    params = AnalysisParams()
    ts, power, temp = build_series([(1500, 60.0, 84.0)], tau_s=0.1, start_temp=84.0)
    windows, _ = detect(ts, power, temp, params)
    assert len(windows) == 3                       # 600 + 600 + 300
    assert all(w.duration_s <= params.window_max_s for w in windows)


def test_power_nan_acts_like_dip():
    """Молчащий сенсор мощности на 2с — как провал: грейс мостит."""
    ts, power, temp = build_series([(60, 60.0, 84.0)], tau_s=0.1, start_temp=84.0)
    power[30:32] = np.nan
    windows, _ = detect(ts, power, temp)
    assert len(windows) == 1


def test_stratum_boundaries():
    assert stratum_of(40.0) == "p35_50"
    assert stratum_of(50.0) == "p50_80"
    assert stratum_of(80.0) == "p80plus"
