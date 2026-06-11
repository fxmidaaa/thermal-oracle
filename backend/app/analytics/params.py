"""Все пороги аналитики — в одном месте (architecture.md §5.3: «в коде констант
нет»). Дефолты — для laptop; per-device переопределения приходят из
devices.analysis_overrides (jsonb), неизвестные ключи игнорируются.
"""
import dataclasses
import json
from dataclasses import dataclass

MODEL_VERSION = 1  # bump → reprocess пересчитает производные


@dataclass(frozen=True)
class AnalysisParams:
    # --- T_ambient (§5.2) ---
    idle_power_w: float = 5.0        # порог скользящего среднего мощности
    idle_power_max_w: float = 8.0    # мгновенный потолок: сэмплы выше исключаются из оценки
    idle_rolling_s: int = 30
    idle_min_duration_s: float = 900.0    # эпизод ≥ 15 мин
    idle_discard_head_s: float = 600.0    # первые 10 мин — soak-back, в мусор
    idle_min_tail_s: float = 300.0        # после отброса должно остаться ≥ 5 мин
    idle_gap_split_s: float = 60.0        # разрыв данных > 60с рвёт эпизод
    ambient_percentile: float = 10.0
    ambient_clamp_low: float = 5.0
    ambient_clamp_high: float = 45.0
    ambient_decay_per_day: float = 0.8    # конфиденс carry-forward оценки
    ambient_max_age_days: int = 7

    # --- стабильные окна нагрузки (§5.3) ---
    load_enter_w: float = 35.0       # гистерезис: вход
    load_exit_w: float = 30.0        # гистерезис: выход
    dip_grace_s: float = 3.0         # провал ≤ 3с не размыкает окно
    window_min_s: float = 15.0
    window_max_s: float = 600.0      # длинные сессии режем для гранулярности
    gap_split_s: float = 2.0         # пропуск сэмплов > 2с рвёт окно
    completeness_min: float = 0.8    # доля фактических сэмплов в окне
    cv_max: float = 0.20             # CV(P) = σ/μ — гейт «пилы»
    tail_dtdt_max: float = 0.15      # |dT/dt| хвоста, °C/с — гейт плато
    tail_s: float = 10.0             # хвост = min(tail_s, duration/3)
    medfilt_s: int = 5               # медианный фильтр температур (выбросы сенсора)

    # --- тренд и здоровье (§5.5) ---
    cusum_k_sigma: float = 0.5
    cusum_h_sigma: float = 5.0
    trend_window_days: int = 90
    baseline_days: int = 14
    baseline_min_windows: int = 30
    current_days: int = 7
    publish_min_days: int = 10       # минимум дней с данными для публикации slope
    publish_min_windows: int = 100
    quality_min: float = 0.5         # окна ниже — не участвуют в тренде
    t_throttle_c: float = 95.0       # дефолт; позже — по cpu_model
    forecast_max_days: int = 180

    # --- диагностика «паста vs пыль» (§5.5) ---
    diag_min_windows: int = 20       # минимум окон в каждом сравниваемом периоде
    diag_rth_growth_pct: float = 8.0     # рост Rth, считающийся деградацией
    diag_rth_flat_pct: float = 5.0       # ниже — Rth «не изменился»
    diag_rpm_shift_pct: float = 10.0     # рост медианных RPM при той же мощности
    diag_high_rpm_quantile: float = 75.0  # «максимальные обороты» = верхний квартиль

    def with_overrides(self, overrides: dict | str | None) -> "AnalysisParams":
        # asyncpg отдаёт jsonb СТРОКОЙ (без set_type_codec) — декодируем здесь,
        # в единственной точке использования
        if isinstance(overrides, str):
            overrides = json.loads(overrides) if overrides.strip() else {}
        if not overrides:
            return self
        known = {f.name for f in dataclasses.fields(self)}
        clean = {k: v for k, v in overrides.items() if k in known}
        return dataclasses.replace(self, **clean) if clean else self
