"""T_ambient: защита от soak-back, пороги длительности, устойчивость к
фоновым всплескам, confidence."""
import numpy as np

from app.analytics.ambient import estimate_day_ambient, find_idle_episodes
from app.analytics.params import AnalysisParams
from tests.analytics.conftest import build_series

P = AnalysisParams()


def test_soak_back_head_fully_discarded():
    """Контракт отброса: первые 10 мин эпизода НЕ влияют на оценку.
    Температура головы абсурдно высокая (99°), хвоста — 30.0 → оценка 30.0."""
    n = 16 * 60
    ts = 1_780_000_000.0 + np.arange(n, dtype=float)
    power = np.full(n, 3.0)
    temp = np.where(np.arange(n) < 600, 99.0, 30.0)
    episodes = find_idle_episodes(ts, power, temp, P)
    assert len(episodes) == 1
    assert episodes[0].estimate == 30.0


def test_cooling_after_load_fixture():
    """Фикстура «остывание после нагрузки»: 20 мин нагрузки, затем 20 мин
    простоя с медленным остыванием τ=150с к 31.5°. Оценка — по успокоившемуся
    хвосту, около истинного прокси-ambient."""
    ts, power, temp = build_series(
        [(1200, 60.0, 85.0), (1200, 3.0, 31.5)], tau_s=150.0, noise_t=0.05
    )
    episodes = find_idle_episodes(ts, power, temp, P)
    assert len(episodes) == 1
    # хвост эпизода (10..20 мин остывания): e^{-4}…e^{-8} — практически 31.5
    assert abs(episodes[0].estimate - 31.5) < 0.5


def test_episode_shorter_than_15min_ignored():
    n = 12 * 60
    ts = np.arange(n, dtype=float)
    episodes = find_idle_episodes(ts, np.full(n, 3.0), np.full(n, 30.0), P)
    assert episodes == []


def test_tail_shorter_than_5min_gives_no_estimate():
    """15.5 мин: хвост 5.5 мин — оценка есть; 14.9 мин — эпизода нет вовсе."""
    n = int(15.5 * 60)
    ts = np.arange(n, dtype=float)
    episodes = find_idle_episodes(ts, np.full(n, 3.0), np.full(n, 30.0), P)
    assert len(episodes) == 1 and episodes[0].estimate is not None

    n = int(14.9 * 60)
    ts = np.arange(n, dtype=float)
    assert find_idle_episodes(ts, np.full(n, 3.0), np.full(n, 30.0), P) == []


def test_background_bursts_do_not_shatter_episode():
    """2-секундные всплески 20 Вт каждую минуту: скользящее среднее остаётся
    < 5 Вт → эпизод ОДИН; сэмплы всплесков исключены из оценки температуры."""
    n = 20 * 60
    ts = np.arange(n, dtype=float)
    power = np.full(n, 3.0)
    temp = np.full(n, 31.5)
    for minute in range(1, 19):
        power[minute * 60 : minute * 60 + 2] = 20.0
        temp[minute * 60 : minute * 60 + 2] = 45.0   # всплеск греет — но он исключён
    episodes = find_idle_episodes(ts, power, temp, P)
    assert len(episodes) == 1
    assert abs(episodes[0].estimate - 31.5) < 0.3


def test_short_mean_excursion_bridged_by_grace():
    """Маска «мерцает» у порога: 20-секундный фоновый burst поднимает
    скользящее среднее над idle_power_w на ~45с — грейс 60с мостит, эпизод
    ОДИН. Без грейса эпизод рассыпался бы на две половины < 15 мин → ноль."""
    n = 26 * 60
    ts = np.arange(n, dtype=float)
    power = np.full(n, 3.0)
    power[13 * 60 : 13 * 60 + 20] = 20.0          # всплеск Docker/индексатора
    temp = np.full(n, 31.5)

    episodes = find_idle_episodes(ts, power, temp, P)
    assert len(episodes) == 1
    assert abs(episodes[0].estimate - 31.5) < 0.3

    no_grace = AnalysisParams(idle_grace_s=0.0)
    assert find_idle_episodes(ts, power, temp, no_grace) == []


def test_desktop_class_profile_with_overrides():
    """Профиль i9-13900HX: простой 13–22 Вт, дефолтные 5 Вт не дают ничего,
    оверрайды (как в devices.analysis_overrides) — дают эпизод и оценку."""
    n = 20 * 60
    ts = np.arange(n, dtype=float)
    rng = np.random.default_rng(13)
    power = rng.uniform(14.0, 21.0, n)             # «idle» большого чипа
    temp = np.full(n, 47.0) + rng.normal(0, 0.2, n)

    assert find_idle_episodes(ts, power, temp, P) == []   # ультрабучный порог слеп

    hx = AnalysisParams().with_overrides({
        "idle_power_w": 22.0, "idle_power_max_w": 28.0,
        "idle_min_duration_s": 300, "idle_discard_head_s": 180,
        "idle_min_tail_s": 120, "ambient_clamp_high": 60.0,
    })
    episodes = find_idle_episodes(ts, power, temp, hx)
    assert len(episodes) == 1
    day = estimate_day_ambient(episodes, hx)
    assert day is not None
    assert abs(day.t_ambient - 47.0) < 0.5         # p10 стабильного хвоста
    assert day.t_ambient < hx.ambient_clamp_high   # кламп поднят — не душит


def test_sustained_activity_breaks_episode():
    """60с устойчивой нагрузки 25 Вт — это уже не простой: эпизод рвётся."""
    n = 40 * 60
    ts = np.arange(n, dtype=float)
    power = np.full(n, 3.0)
    power[19 * 60 : 20 * 60] = 25.0
    temp = np.full(n, 31.0)
    episodes = find_idle_episodes(ts, power, temp, P)
    assert len(episodes) == 2                        # две половины по ~19 мин


def test_day_aggregate_confidence():
    n = 20 * 60
    ts = np.arange(n, dtype=float)
    episodes = find_idle_episodes(ts, np.full(n, 3.0), np.full(n, 30.5), P)
    day = estimate_day_ambient(episodes, P)
    assert day is not None
    assert abs(day.t_ambient - 30.5) < 0.1
    assert day.idle_minutes == 20
    assert 0.2 < day.confidence < 0.5               # 20 мин простоя — умеренная вера


def test_day_aggregate_clamps_implausible_values():
    n = 20 * 60
    ts = np.arange(n, dtype=float)
    episodes = find_idle_episodes(ts, np.full(n, 3.0), np.full(n, 2.0), P)  # «2°C в комнате»
    day = estimate_day_ambient(episodes, P)
    assert day.t_ambient == P.ambient_clamp_low
    assert day.confidence < 0.25                    # клампнутой оценке веры вдвое меньше


def test_no_idle_returns_none():
    assert estimate_day_ambient([], P) is None
