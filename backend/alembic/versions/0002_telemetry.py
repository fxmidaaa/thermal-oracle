"""Гипертаблица telemetry_raw + журнал батчей + очередь пересчёта.

См. docs/architecture.md §4.2–4.4. Ключевой инвариант:
compress_after сырой телеметрии (7 дней) == горизонт приёма опоздавших данных
в ingest (LATE_HORIZON). Вставка в сжатые чанки дорогая, поэтому всё
«опоздавшее» должно успеть приехать до компрессии.
"""
from alembic import op

revision = "0002_telemetry"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE telemetry_raw (
            device_id  uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            ts         timestamptz NOT NULL,
            cpu_temp   real,
            gpu_temp   real,
            cpu_power  real,
            gpu_power  real,
            fan_rpm    integer,
            process_id integer REFERENCES processes(id),
            flags      smallint NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        SELECT create_hypertable('telemetry_raw', 'ts',
                                 chunk_time_interval => INTERVAL '1 day')
    """)
    # Слой 2 дедупликации: ровно одна строка на (устройство, момент времени).
    # Уникальный индекс гипертаблицы обязан включать партиционирующую колонку — ок.
    op.execute("""
        CREATE UNIQUE INDEX uq_telemetry_raw_device_ts
            ON telemetry_raw (device_id, ts)
    """)
    op.execute("""
        ALTER TABLE telemetry_raw SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'device_id',
            timescaledb.compress_orderby = 'ts DESC'
        )
    """)
    op.execute(
        "SELECT add_compression_policy('telemetry_raw', compress_after => INTERVAL '7 days')"
    )
    op.execute(
        "SELECT add_retention_policy('telemetry_raw', drop_after => INTERVAL '30 days')"
    )

    # Слой 1 дедупликации: идемпотентность целого батча по PK (device_id, batch_id).
    # ОБЫЧНАЯ таблица, не гипертаблица: уникальный индекс гипертаблицы обязан
    # включать партиционирующую колонку времени, что сломало бы семантику PK.
    # Чистка — ночным DELETE по received_at (часть worker'а); при масштабе
    # >500 активных устройств решение пересмотреть (см. architecture.md §4.2).
    op.execute("""
        CREATE TABLE ingest_batches (
            device_id      uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            batch_id       uuid NOT NULL,
            received_at    timestamptz NOT NULL DEFAULT now(),
            sent_at        timestamptz,
            sample_count   integer NOT NULL,
            dup_count      integer NOT NULL DEFAULT 0,
            rejected_count integer NOT NULL DEFAULT 0,
            agent_version  text,
            PRIMARY KEY (device_id, batch_id)
        )
    """)
    op.execute("CREATE INDEX ix_ingest_batches_received ON ingest_batches (received_at)")

    # Данные старше watermark аналитики попали в raw → диапазон на пересчёт
    # (architecture.md §4.4). Потребитель — analytics worker (следующие шаги).
    op.execute("""
        CREATE TABLE reprocess_queue (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            device_id    uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            range_start  timestamptz NOT NULL,
            range_end    timestamptz NOT NULL,
            enqueued_at  timestamptz NOT NULL DEFAULT now(),
            processed_at timestamptz
        )
    """)
    op.execute("""
        CREATE INDEX ix_reprocess_pending ON reprocess_queue (enqueued_at)
            WHERE processed_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reprocess_queue")
    op.execute("DROP TABLE IF EXISTS ingest_batches")
    op.execute("DROP TABLE IF EXISTS telemetry_raw CASCADE")
