"""E2E сэмплера без настоящего LHM: локальный HTTP-сервер раздаёт фикстуру
дерева, Sampler гоняет реальный цикл HTTP → extract → SQLite."""
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from thermal_agent.collectors.windows_lhm import LhmCollector
from thermal_agent.config import AgentConfig
from thermal_agent.sampler import Sampler
from thermal_agent.spool import Spool

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def fake_lhm_url():
    handler = partial(SimpleHTTPRequestHandler, directory=str(DATA_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/lhm_sample.json"
    server.shutdown()
    thread.join(timeout=5)


def test_sampler_collects_real_http_into_spool(tmp_path, fake_lhm_url):
    cfg = AgentConfig(
        lhm_url=fake_lhm_url,
        sample_interval_s=0.05,  # ускоренный «секундный» цикл
        spool_path=str(tmp_path / "spool.db"),
    )
    stop = threading.Event()
    collector = LhmCollector(cfg.lhm_url)
    sampler_thread = threading.Thread(target=Sampler(cfg, collector, stop).loop, daemon=True)
    sampler_thread.start()
    time.sleep(0.6)
    stop.set()
    sampler_thread.join(timeout=5)
    collector.close()

    spool = Spool(cfg.spool_path)
    try:
        total, claimed = spool.counts()
        assert total >= 3            # цикл крутился и писал
        assert claimed == 0
        _, samples = spool.claim(10)
        first = samples[0]
        assert first.cpu_temp == 62.0    # правила выбора отработали на фикстуре
        assert first.cpu_power == 45.2
        assert first.gpu_temp == 64.0
        assert first.fan_rpm == 3540
    finally:
        spool.close()


def test_sampler_writes_gap_when_lhm_down(tmp_path):
    """LHM недоступен → честный разрыв ряда, а не фиктивные строки."""
    cfg = AgentConfig(
        lhm_url="http://127.0.0.1:1/data.json",  # заведомо мёртвый порт
        sample_interval_s=0.05,
        spool_path=str(tmp_path / "spool.db"),
    )
    stop = threading.Event()
    collector = LhmCollector(cfg.lhm_url, timeout=0.1)
    sampler_thread = threading.Thread(target=Sampler(cfg, collector, stop).loop, daemon=True)
    sampler_thread.start()
    time.sleep(0.4)
    stop.set()
    sampler_thread.join(timeout=5)
    collector.close()

    spool = Spool(cfg.spool_path)
    try:
        assert spool.counts() == (0, 0)
    finally:
        spool.close()
