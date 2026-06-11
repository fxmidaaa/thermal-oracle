"""with_overrides: jsonb-строка из asyncpg, неизвестные ключи, типы."""
import dataclasses

from app.analytics.params import AnalysisParams


def test_overrides_from_dict():
    p = AnalysisParams().with_overrides({"idle_power_w": 22.0, "idle_power_max_w": 28.0})
    assert p.idle_power_w == 22.0
    assert p.idle_power_max_w == 28.0
    assert p.idle_min_duration_s == 900.0          # незатронутое — дефолт


def test_overrides_from_jsonb_string():
    """asyncpg отдаёт jsonb строкой — реальный формат из devices."""
    raw = '{"idle_power_w": 22.0, "idle_min_duration_s": 300}'
    p = AnalysisParams().with_overrides(raw)
    assert p.idle_power_w == 22.0
    assert p.idle_min_duration_s == 300


def test_empty_jsonb_string_is_noop():
    base = AnalysisParams()
    assert base.with_overrides("{}") == base
    assert base.with_overrides("") == base
    assert base.with_overrides(None) == base


def test_unknown_keys_ignored_known_applied():
    """Кейс с поля: пользователь пишет idle_duration_min вместо
    idle_min_duration_s — неизвестное игнорируется (с warning в лог),
    известное применяется, дефолты не ломаются."""
    p = AnalysisParams().with_overrides(
        '{"idle_power_w": 22.0, "idle_warmup_min": 3, "idle_duration_min": 5}'
    )
    assert p.idle_power_w == 22.0
    assert p.idle_min_duration_s == 900.0          # кривой ключ НЕ сработал
    assert p.idle_discard_head_s == 600.0


def test_overrides_do_not_mutate_base():
    base = AnalysisParams()
    base.with_overrides({"idle_power_w": 22.0})
    assert base.idle_power_w == 5.0                # frozen dataclass + replace


def test_every_idle_param_is_overridable():
    """Контракт «в коде констант нет»: каждый idle-порог переопределяем."""
    idle_fields = [f.name for f in dataclasses.fields(AnalysisParams) if "idle" in f.name]
    assert set(idle_fields) >= {
        "idle_power_w", "idle_power_max_w", "idle_grace_s",
        "idle_min_duration_s", "idle_discard_head_s", "idle_min_tail_s",
    }
    overrides = {name: 111.0 for name in idle_fields}
    p = AnalysisParams().with_overrides(overrides)
    assert all(getattr(p, name) == 111.0 for name in idle_fields)
