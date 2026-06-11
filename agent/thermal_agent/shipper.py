"""Шиппер: фоновый поток, раз в 30 с забирает накопленное из спула и шлёт
POST /v1/telemetry (gzip + Bearer). Строки удаляются из спула ТОЛЬКО после
2xx (включая «duplicate» — серверный дедуп считается успехом).

Обработка ошибок:
- сеть / 5xx / 408 / 429 → экспоненциальный backoff 1с → ×2 → cap 300с
  с джиттером; Retry-After сервера уважается (backpressure из architecture.md
  §6); батч остаётся закреплённым и ретраится С ТЕМ ЖЕ batch_id;
- 401/403 → токен отозван/невалиден: данные не теряем, паркуемся на 5 мин
  (пользователь чинит register), громкая ошибка в лог;
- прочие 4xx (400/413/422) → «отравленный» батч: контракт нарушен (баг агента),
  вечный ретрай заклинил бы весь спул — батч дропается с громким логом
  (потеря ограничена ≤120 сэмплами и видна).

Штатно за 30 с накапливается ~30 строк → батч ≈30 сэмплов. После оффлайна
бэклог дренируется ускоренно: батчи укрупняются до лимита контракта (120)
и уходят без паузы 30 с, пока бэклог не рассосётся.
"""
import gzip
import json
import logging
import random
import threading

import httpx

from thermal_agent.config import AgentConfig
from thermal_agent.models import MAX_BATCH_SAMPLES, build_batch_payload, encode_batch
from thermal_agent.spool import Spool

log = logging.getLogger(__name__)

BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 300.0
AUTH_PARK_S = 300.0
DRAIN_PAUSE_S = 0.2


class Shipper(threading.Thread):
    def __init__(
        self,
        cfg: AgentConfig,
        stop_event: threading.Event,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(name="shipper", daemon=True)
        self.cfg = cfg
        self._stop = stop_event
        self._transport = transport
        self._fail_count = 0
        self._spool: Spool | None = None
        self._client: httpx.Client | None = None
        self.last_result: str = "ещё не отправлял"

    # --- ленивая инициализация: соединение SQLite живёт в потоке-владельце ---
    def _get_spool(self) -> Spool:
        if self._spool is None:
            self._spool = Spool(self.cfg.spool_path, self.cfg.spool_max_hours)
        return self._spool

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10.0, transport=self._transport)
        return self._client

    def run(self) -> None:
        # первый тик сразу: после рестарта дослать pending-батч прошлой сессии
        delay = 0.0
        while not self._stop.wait(delay):
            try:
                delay = self._tick()
            except Exception:  # noqa: BLE001 — поток не должен умирать молча
                log.exception("неожиданная ошибка шиппера")
                delay = self._backoff_delay()
        if self._client is not None:
            self._client.close()
        if self._spool is not None:
            self._spool.close()

    def _tick(self) -> float:
        """Одна попытка отправки. Возвращает паузу до следующего тика (сек)."""
        spool = self._get_spool()
        batch = spool.pending_batch(MAX_BATCH_SAMPLES) or spool.claim(MAX_BATCH_SAMPLES)
        if batch is None:
            return self.cfg.ship_interval_s
        batch_id, samples = batch

        body = encode_batch(build_batch_payload(samples, batch_id))
        try:
            response = self._get_client().post(
                self.cfg.api_url.rstrip("/") + "/v1/telemetry",
                content=body,
                headers={
                    "Authorization": f"Bearer {self.cfg.device_token}",
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            )
        except httpx.HTTPError as exc:
            self._fail_count += 1
            delay = self._backoff_delay()
            self.last_result = f"сеть: {exc.__class__.__name__}"
            log.warning("сеть недоступна (%s), ретрай через %.0f с; батч %s остаётся в спуле",
                        exc.__class__.__name__, delay, batch_id)
            return delay

        if response.is_success:
            spool.delete_batch(batch_id)
            self._fail_count = 0
            counts = self._safe_json(response)
            self.last_result = f"ok: {counts}"
            log.info("батч %s (%d сэмплов) принят: %s", batch_id, len(samples), counts)
            total, _ = spool.counts()
            if total >= MAX_BATCH_SAMPLES:  # дренаж бэклога без паузы 30 с
                return DRAIN_PAUSE_S
            return self.cfg.ship_interval_s

        if response.status_code in (401, 403):
            self.last_result = f"auth {response.status_code}"
            log.error("токен отклонён (%d) — проверьте `thermal-agent register`; "
                      "данные копятся в спуле", response.status_code)
            return AUTH_PARK_S

        if response.status_code in (408, 429) or response.status_code >= 500:
            self._fail_count += 1
            delay = self._retry_after(response) or self._backoff_delay()
            self.last_result = f"http {response.status_code}"
            log.warning("сервер ответил %d, ретрай через %.0f с", response.status_code, delay)
            return delay

        # 400/413/422 и прочие 4xx: отравленный батч
        spool.delete_batch(batch_id)
        self._fail_count = 0
        self.last_result = f"батч отброшен ({response.status_code})"
        log.error("сервер отверг батч %s (%d): %s — батч отброшен (баг контракта?)",
                  batch_id, response.status_code, response.text[:500])
        return 1.0

    def _backoff_delay(self) -> float:
        base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (self._fail_count - 1))
        return base * random.uniform(0.5, 1.5)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        try:
            return float(value) if value else None
        except ValueError:
            return None

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        try:
            return response.json()
        except json.JSONDecodeError:
            return {}


def gunzip_payload(body: bytes) -> dict:
    """Обратная сторона encode_batch — используется тестами."""
    return json.loads(gzip.decompress(body))
