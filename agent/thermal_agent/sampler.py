"""Сэмплер: цикл 1 Гц в главном потоке.

Планирование по time.monotonic (без накопления дрейфа). Пауза — time.sleep,
а не Event.wait: на Windows Event.wait в главном потоке не прерывается
Ctrl+C, а time.sleep — прерывается. После сна ноутбука/глубокого отставания
расписание пересинхронизируется (пропущенные тики НЕ навёрстываются — burst
из одинаковых сэмплов хуже честного разрыва ряда).
"""
import logging
import time

from thermal_agent.collectors.base import Collector
from thermal_agent.config import AgentConfig
from thermal_agent.spool import Spool

log = logging.getLogger(__name__)

_RESYNC_AFTER_MISSED = 5  # тиков отставания → пересинхронизация (сон/гибернация)
_STATS_EVERY_TICKS = 60


class Sampler:
    def __init__(self, cfg: AgentConfig, collector: Collector, stop_flag):
        self.cfg = cfg
        self.collector = collector
        self._stop = stop_flag  # threading.Event; здесь только .is_set()

    def loop(self) -> None:
        spool = Spool(self.cfg.spool_path, self.cfg.spool_max_hours)
        interval = self.cfg.sample_interval_s
        next_tick = time.monotonic()
        ticks = collected = 0
        try:
            while not self._stop.is_set():
                sample = self.collector.sample()
                if sample is not None:
                    spool.insert(sample)
                    collected += 1
                ticks += 1

                if ticks % _STATS_EVERY_TICKS == 0:
                    purged = spool.purge_old()
                    total, claimed = spool.counts()
                    log.info(
                        "за минуту: %d/%d сэмплов; спул: %d строк (в полёте %d%s)",
                        collected, _STATS_EVERY_TICKS, total, claimed,
                        f", ретеншн удалил {purged}" if purged else "",
                    )
                    collected = 0

                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay < -_RESYNC_AFTER_MISSED * interval:
                    log.info("обнаружен разрыв %.0f с (сон/нагрузка) — пересинхронизация",
                             -delay)
                    next_tick = time.monotonic() + interval
                    delay = interval
                if delay > 0:
                    time.sleep(delay)
        finally:
            spool.close()
