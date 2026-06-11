"""Шиппер против httpx.MockTransport: семантика 2xx/5xx/4xx, переиспользование
batch_id при ретраях, backoff, дренаж бэклога. Поток не запускается —
_tick() вызывается напрямую."""
import json
import threading
import time

import httpx
import pytest

from thermal_agent.config import AgentConfig
from thermal_agent.models import MAX_BATCH_SAMPLES, Sample
from thermal_agent.shipper import BACKOFF_CAP_S, Shipper, gunzip_payload
from thermal_agent.spool import Spool

NOW_MS = time.time_ns() // 1_000_000
OK_BODY = {"accepted": 30, "duplicates": 0, "rejected": 0, "status": "ok"}


def make_shipper(tmp_path, responses: list[httpx.Response], requests_log: list):
    """Шиппер с MockTransport, отдающим заготовленные ответы по очереди."""
    def handler(request: httpx.Request) -> httpx.Response:
        requests_log.append(gunzip_payload(request.content))
        return responses.pop(0)

    cfg = AgentConfig(
        api_url="http://backend.test",
        device_token="to_test_token_aaaa",
        spool_path=str(tmp_path / "spool.db"),
    )
    shipper = Shipper(cfg, threading.Event(), transport=httpx.MockTransport(handler))
    return shipper, Spool(cfg.spool_path)


def fill(spool: Spool, n: int) -> None:
    for i in range(n):
        spool.insert(Sample(ts_ms=NOW_MS + i * 1000, cpu_temp=80.0, cpu_power=50.0))


def test_success_deletes_rows_and_waits_interval(tmp_path):
    sent = []
    shipper, spool = make_shipper(tmp_path, [httpx.Response(200, json=OK_BODY)], sent)
    fill(spool, 30)
    delay = shipper._tick()
    assert delay == shipper.cfg.ship_interval_s
    assert spool.counts() == (0, 0)
    payload = sent[0]
    assert payload["schema_version"] == 1
    assert len(payload["samples"]) == 30


def test_retry_after_5xx_reuses_same_batch_id(tmp_path):
    """Главный контракт идемпотентности: ретрай == тот же batch_id."""
    sent = []
    shipper, spool = make_shipper(
        tmp_path,
        [httpx.Response(503), httpx.Response(200, json=OK_BODY)],
        sent,
    )
    fill(spool, 30)

    delay1 = shipper._tick()           # 503 → батч остаётся закреплённым
    assert spool.counts() == (30, 30)
    assert delay1 > 0

    delay2 = shipper._tick()           # ретрай → успех
    assert spool.counts() == (0, 0)
    assert delay2 == shipper.cfg.ship_interval_s
    assert sent[0]["batch_id"] == sent[1]["batch_id"]


def test_duplicate_response_treated_as_success(tmp_path):
    sent = []
    dup = {"accepted": 0, "duplicates": 0, "rejected": 0, "status": "duplicate"}
    shipper, spool = make_shipper(tmp_path, [httpx.Response(200, json=dup)], sent)
    fill(spool, 10)
    shipper._tick()
    assert spool.counts() == (0, 0)    # серверный дедуп == доставлено, спул чистим


def test_network_error_keeps_rows_and_backs_off(tmp_path):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    cfg = AgentConfig(
        api_url="http://backend.test", device_token="to_x",
        spool_path=str(tmp_path / "spool.db"),
    )
    shipper = Shipper(cfg, threading.Event(), transport=httpx.MockTransport(handler))
    spool = Spool(cfg.spool_path)
    fill(spool, 30)

    delays = [shipper._tick() for _ in range(7)]
    assert spool.counts() == (30, 30)              # ничего не потеряли
    assert delays[2] > delays[0]                   # backoff растёт...
    assert all(d <= BACKOFF_CAP_S * 1.5 for d in delays)  # ...и упирается в cap


def test_poison_batch_dropped_on_422(tmp_path):
    sent = []
    shipper, spool = make_shipper(
        tmp_path, [httpx.Response(422, json={"detail": "validation error"})], sent
    )
    fill(spool, 5)
    shipper._tick()
    assert spool.counts() == (0, 0)    # отравленный батч не клинит спул навечно


def test_auth_failure_parks_without_data_loss(tmp_path):
    sent = []
    shipper, spool = make_shipper(tmp_path, [httpx.Response(401)], sent)
    fill(spool, 5)
    delay = shipper._tick()
    assert delay == pytest.approx(300.0)
    assert spool.counts() == (5, 5)    # данные ждут починки токена


def test_backlog_drains_with_big_batches_and_no_pause(tmp_path):
    sent = []
    responses = [httpx.Response(200, json=OK_BODY) for _ in range(3)]
    shipper, spool = make_shipper(tmp_path, responses, sent)
    fill(spool, 250)                    # бэклог после «оффлайна»

    d1 = shipper._tick()
    assert len(sent[0]["samples"]) == MAX_BATCH_SAMPLES   # батч укрупнился до 120
    assert d1 < 1.0                                       # и пауза — дренажная

    shipper._tick()
    assert len(sent[1]["samples"]) == MAX_BATCH_SAMPLES

    d3 = shipper._tick()                                  # хвост 10 строк
    assert len(sent[2]["samples"]) == 10
    assert d3 == shipper.cfg.ship_interval_s
    assert spool.counts() == (0, 0)


def test_retry_after_header_respected(tmp_path):
    sent = []
    shipper, spool = make_shipper(
        tmp_path, [httpx.Response(429, headers={"Retry-After": "42"})], sent
    )
    fill(spool, 5)
    assert shipper._tick() == pytest.approx(42.0)
    assert spool.counts() == (5, 5)


def test_request_wire_format(tmp_path):
    """Заголовки и тело — ровно то, что ждёт бэкенд-middleware."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["payload"] = gunzip_payload(request.content)
        return httpx.Response(200, json=OK_BODY)

    cfg = AgentConfig(
        api_url="http://backend.test/",  # хвостовой слэш не должен ломать URL
        device_token="to_test_token_aaaa",
        spool_path=str(tmp_path / "spool.db"),
    )
    shipper = Shipper(cfg, threading.Event(), transport=httpx.MockTransport(handler))
    spool = Spool(cfg.spool_path)
    spool.insert(Sample(ts_ms=NOW_MS, cpu_temp=50.5, process="game.exe"))
    shipper._tick()

    headers = captured["headers"]
    assert headers["authorization"] == "Bearer to_test_token_aaaa"
    assert headers["content-encoding"] == "gzip"
    assert headers["content-type"] == "application/json"
    sample = captured["payload"]["samples"][0]
    assert sample["process"] == "game.exe"
    assert "gpu_temp" not in sample    # None-поля не сериализуются
    assert json.dumps(captured["payload"])  # тело — валидный JSON-словарь
