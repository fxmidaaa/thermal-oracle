"""Примитивы работы с 1 Гц рядами (NumPy, NaN-aware).

Конвенции: время — float unix-секунды UTC; отсутствие показания — NaN;
длительность отрезка [i0, i1) считается как ts[i1-1] − ts[i0] + 1.0
(номинальный шаг 1 с — иначе окно из 15 сэмплов имело бы «14 секунд»).
"""
import numpy as np

NOMINAL_STEP_S = 1.0


def span_s(ts: np.ndarray, i0: int, i1: int) -> float:
    """Длительность полуинтервала [i0, i1) в секундах."""
    return float(ts[i1 - 1] - ts[i0]) + NOMINAL_STEP_S


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Непрерывные участки True → [(start, end)) — векторно, без циклов по сэмплам."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist(), strict=True))


def split_on_gaps(ts: np.ndarray, max_gap_s: float) -> list[tuple[int, int]]:
    """Сегменты [(i0, i1)) без пропусков сэмплов длиннее max_gap_s."""
    if ts.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(ts) > max_gap_s) + 1
    bounds = np.concatenate(([0], breaks, [ts.size]))
    return list(zip(bounds[:-1].tolist(), bounds[1:].tolist(), strict=True))


def _sliding(values: np.ndarray, width: int) -> np.ndarray:
    """Скользящее окно с NaN-паддингом краёв → (n, width)."""
    if width % 2 == 0:
        width += 1
    half = width // 2
    padded = np.concatenate((np.full(half, np.nan), values, np.full(half, np.nan)))
    return np.lib.stride_tricks.sliding_window_view(padded, width)


def median_filter(values: np.ndarray, width_s: int) -> np.ndarray:
    """Медианный фильтр (одиночные выбросы сенсора, §5.3); NaN прозрачны."""
    if values.size == 0 or width_s <= 1:
        return values.copy()
    windows = _sliding(values, width_s)
    all_nan = np.all(np.isnan(windows), axis=1)
    out = np.full(values.shape, np.nan)
    if not all_nan.all():
        out[~all_nan] = np.nanmedian(windows[~all_nan], axis=1)
    return out


def rolling_mean(values: np.ndarray, width_s: int) -> np.ndarray:
    """NaN-aware скользящее среднее (детекция idle по сглаженной мощности)."""
    if values.size == 0:
        return values.copy()
    windows = _sliding(values, width_s)
    all_nan = np.all(np.isnan(windows), axis=1)
    out = np.full(values.shape, np.nan)
    if not all_nan.all():
        out[~all_nan] = np.nanmean(windows[~all_nan], axis=1)
    return out


def bridge_short_gaps(mask: np.ndarray, ts: np.ndarray, max_gap_s: float) -> np.ndarray:
    """False-раны длительностью ≤ max_gap_s СТРОГО МЕЖДУ True-ранами → True.

    Один примитив на два применения: провалы мощности внутри окна нагрузки
    (грейс 3с) и выбросы скользящего среднего над idle-порогом (грейс 60с —
    без него маска «мерцает», когда медиана мощности ходит у самого порога,
    как у i9-13900HX с фоновым Docker). Краевые False-раны не мостим:
    окно/эпизод не должны начинаться или заканчиваться выбросом."""
    if max_gap_s <= 0:
        return mask.copy()
    bridged = mask.copy()
    for f0, f1 in runs(~mask):
        if f0 == 0 or f1 == mask.size:
            continue
        if span_s(ts, f0, f1) <= max_gap_s:
            bridged[f0:f1] = True
    return bridged


def geometric_mean(scores: list[float], floor: float = 0.05) -> float:
    """Композит качества: геом. среднее субскоров, каждый прижат к [floor, 1]."""
    clipped = np.clip(np.asarray(scores, dtype=float), floor, 1.0)
    return float(np.exp(np.mean(np.log(clipped))))
