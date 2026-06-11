"""SQLite-спул: гарантия недоставки-без-потери (architecture.md §2.2).

Каждый сэмпл сначала падает на диск; сеть/бэкенд/сон ноутбука трогают только
отправку. WAL + busy_timeout позволяют двум потокам (сэмплер пишет, шиппер
читает/удаляет) работать с одним файлом — но КАЖДЫЙ поток создаёт свой
экземпляр Spool: соединение sqlite3 не пересекает границы потоков.

Дисциплина batch_id (основа идемпотентности слоя 1 на сервере):
- claim() закрепляет за строками НОВЫЙ batch_id;
- pending_batch() возвращает уже закреплённый — после краша/ошибки сети ретрай
  уходит С ТЕМ ЖЕ batch_id, и сервер схлопывает повтор;
- delete_batch() — только после 2xx (включая ответ «duplicate»).
Инвариант single-flight: в полёте не больше одного batch_id.

Ретеншн: строки старше spool_max_hours удаляются (по умолчанию 24 ч ≈ 86 400
строк ≈ единицы МБ) — спул не забьёт диск пользователя при долгом оффлайне.
"""
import sqlite3
import time
from pathlib import Path

from thermal_agent.models import Sample, uuid7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts_ms     INTEGER PRIMARY KEY,  -- PK даёт локальную дедупликацию по времени
    cpu_temp  REAL,
    gpu_temp  REAL,
    cpu_power REAL,
    gpu_power REAL,
    fan_rpm   INTEGER,
    process   TEXT,
    batch_id  TEXT                  -- NULL = свободна; иначе закреплена за батчем
);
CREATE INDEX IF NOT EXISTS ix_samples_batch ON samples (batch_id);
"""

_COLUMNS = "ts_ms, cpu_temp, gpu_temp, cpu_power, gpu_power, fan_rpm, process"


class Spool:
    def __init__(self, path: str, max_hours: float = 24.0):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._max_hours = max_hours
        self._conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def insert(self, sample: Sample) -> None:
        self._conn.execute(
            f"INSERT OR IGNORE INTO samples ({_COLUMNS}) VALUES (?,?,?,?,?,?,?)",
            (sample.ts_ms, sample.cpu_temp, sample.gpu_temp, sample.cpu_power,
             sample.gpu_power, sample.fan_rpm, sample.process),
        )

    def purge_old(self, now_ms: int | None = None) -> int:
        now_ms = now_ms or time.time_ns() // 1_000_000
        cutoff = now_ms - int(self._max_hours * 3_600_000)
        cursor = self._conn.execute("DELETE FROM samples WHERE ts_ms < ?", (cutoff,))
        return cursor.rowcount

    def pending_batch(self, limit: int) -> tuple[str, list[Sample]] | None:
        row = self._conn.execute(
            "SELECT batch_id FROM samples WHERE batch_id IS NOT NULL LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._rows_of(row["batch_id"], limit)

    def claim(self, limit: int) -> tuple[str, list[Sample]] | None:
        batch_id = str(uuid7())
        cursor = self._conn.execute(
            """UPDATE samples SET batch_id = ?
               WHERE ts_ms IN (SELECT ts_ms FROM samples WHERE batch_id IS NULL
                               ORDER BY ts_ms LIMIT ?)""",
            (batch_id, limit),
        )
        if cursor.rowcount == 0:
            return None
        return self._rows_of(batch_id, limit)

    def _rows_of(self, batch_id: str, limit: int) -> tuple[str, list[Sample]] | None:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM samples WHERE batch_id = ? ORDER BY ts_ms LIMIT ?",
            (batch_id, limit),
        ).fetchall()
        if not rows:  # ретеншн успел вычистить закреплённые строки
            return None
        return batch_id, [Sample(**dict(row)) for row in rows]

    def delete_batch(self, batch_id: str) -> None:
        self._conn.execute("DELETE FROM samples WHERE batch_id = ?", (batch_id,))

    def counts(self) -> tuple[int, int]:
        """→ (всего строк, из них закреплено за батчем)."""
        row = self._conn.execute(
            "SELECT count(*) AS total, count(batch_id) AS claimed FROM samples"
        ).fetchone()
        return row["total"], row["claimed"]

    def oldest_ts_ms(self) -> int | None:
        row = self._conn.execute("SELECT min(ts_ms) AS m FROM samples").fetchone()
        return row["m"]

    def close(self) -> None:
        self._conn.close()
