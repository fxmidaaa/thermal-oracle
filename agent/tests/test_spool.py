"""Спул: FIFO-claim, single-flight batch_id, ретеншн, локальная дедупликация."""
import time

from thermal_agent.models import MAX_BATCH_SAMPLES, Sample
from thermal_agent.spool import Spool

NOW_MS = time.time_ns() // 1_000_000


def fill(spool: Spool, n: int, start_ms: int = NOW_MS) -> None:
    for i in range(n):
        spool.insert(Sample(ts_ms=start_ms + i * 1000, cpu_temp=50.0, cpu_power=10.0))


def make_spool(tmp_path, max_hours=24.0) -> Spool:
    return Spool(str(tmp_path / "spool.db"), max_hours)


def test_claim_takes_oldest_first_up_to_limit(tmp_path):
    spool = make_spool(tmp_path)
    fill(spool, 200)
    batch_id, samples = spool.claim(MAX_BATCH_SAMPLES)
    assert len(samples) == 120
    assert samples[0].ts_ms == NOW_MS                      # FIFO: бэклог по порядку
    assert samples[-1].ts_ms == NOW_MS + 119_000
    assert spool.counts() == (200, 120)


def test_pending_batch_returns_same_batch_id(tmp_path):
    """Краш/ошибка сети → ретрай обязан уйти с ТЕМ ЖЕ batch_id (слой 1 дедупа)."""
    spool = make_spool(tmp_path)
    fill(spool, 30)
    batch_id, _ = spool.claim(MAX_BATCH_SAMPLES)

    again = spool.pending_batch(MAX_BATCH_SAMPLES)
    assert again is not None
    assert again[0] == batch_id

    # ...в том числе из нового экземпляра (рестарт процесса агента)
    reopened = make_spool(tmp_path)
    after_restart = reopened.pending_batch(MAX_BATCH_SAMPLES)
    assert after_restart is not None
    assert after_restart[0] == batch_id


def test_delete_batch_then_next_claim(tmp_path):
    spool = make_spool(tmp_path)
    fill(spool, 150)
    batch_id, _ = spool.claim(MAX_BATCH_SAMPLES)
    spool.delete_batch(batch_id)
    assert spool.counts() == (30, 0)
    next_id, samples = spool.claim(MAX_BATCH_SAMPLES)
    assert next_id != batch_id
    assert len(samples) == 30
    assert samples[0].ts_ms == NOW_MS + 120_000


def test_claim_empty_spool_returns_none(tmp_path):
    assert make_spool(tmp_path).claim(MAX_BATCH_SAMPLES) is None
    assert make_spool(tmp_path).pending_batch(MAX_BATCH_SAMPLES) is None


def test_duplicate_ts_ignored_locally(tmp_path):
    spool = make_spool(tmp_path)
    sample = Sample(ts_ms=NOW_MS, cpu_temp=50.0)
    spool.insert(sample)
    spool.insert(sample)
    assert spool.counts() == (1, 0)


def test_purge_old_respects_retention(tmp_path):
    spool = make_spool(tmp_path, max_hours=24.0)
    old = NOW_MS - 25 * 3_600_000   # 25 часов назад — за ретеншном
    fresh = NOW_MS - 1 * 3_600_000  # час назад — внутри
    spool.insert(Sample(ts_ms=old, cpu_temp=40.0))
    spool.insert(Sample(ts_ms=fresh, cpu_temp=41.0))
    purged = spool.purge_old(now_ms=NOW_MS)
    assert purged == 1
    assert spool.counts() == (1, 0)
    assert spool.oldest_ts_ms() == fresh
