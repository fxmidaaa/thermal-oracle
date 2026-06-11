"""Реляционное ядро: users, devices, tokens, журнал обслуживания, processes, alerts.

См. docs/architecture.md §4.1.
"""
from alembic import op

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # В dev-образе timescale/timescaledb расширение уже преинсталлировано в БД;
    # IF NOT EXISTS делает миграцию идемпотентной. В проде расширения ставит
    # provisioning (нужны права суперпользователя).
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.execute("""
        CREATE TABLE users (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email         citext NOT NULL UNIQUE,
            password_hash text,
            plan          text NOT NULL DEFAULT 'free',
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE devices (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name               text NOT NULL,
            platform           text NOT NULL CHECK (platform IN ('windows', 'macos')),
            device_class       text NOT NULL DEFAULT 'laptop'
                               CHECK (device_class IN ('laptop', 'desktop')),
            cpu_model          text,
            gpu_model          text,
            capabilities       jsonb NOT NULL DEFAULT '{}',
            sensor_map         jsonb NOT NULL DEFAULT '{}',
            analysis_overrides jsonb NOT NULL DEFAULT '{}',
            agent_version      text,
            timezone           text NOT NULL DEFAULT 'UTC',
            created_at         timestamptz NOT NULL DEFAULT now(),
            last_seen_at       timestamptz
        )
    """)
    op.execute("CREATE INDEX ix_devices_user ON devices (user_id)")

    op.execute("""
        CREATE TABLE device_tokens (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id    uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            token_prefix text NOT NULL,
            token_hash   bytea NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz,
            revoked_at   timestamptz
        )
    """)
    op.execute("CREATE INDEX ix_device_tokens_prefix ON device_tokens (token_prefix)")

    # Словарь имён процессов: телеметрия хранит узкий int вместо text.
    op.execute("""
        CREATE TABLE processes (
            id   serial PRIMARY KEY,
            name text NOT NULL UNIQUE
        )
    """)

    # Журнал обслуживания режет историю устройства на «эпохи»: replaste
    # обнуляет базлайн деградации (architecture.md §5.5).
    op.execute("""
        CREATE TABLE maintenance_events (
            id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            device_id uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            ts        timestamptz NOT NULL,
            kind      text NOT NULL CHECK (kind IN
                      ('repaste', 'cleaning', 'fan_curve_change',
                       'undervolt_change', 'hw_change')),
            tim_type  text CHECK (tim_type IN
                      ('paste', 'liquid_metal', 'ptm7950', 'stock', 'unknown')),
            source    text NOT NULL DEFAULT 'user',
            note      text
        )
    """)
    op.execute("CREATE INDEX ix_maintenance_device_ts ON maintenance_events (device_id, ts)")

    op.execute("""
        CREATE TABLE alerts (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            device_id  uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            kind       text NOT NULL,
            severity   text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
            created_at timestamptz NOT NULL DEFAULT now(),
            payload    jsonb NOT NULL DEFAULT '{}',
            acked_at   timestamptz
        )
    """)
    op.execute("CREATE INDEX ix_alerts_device_created ON alerts (device_id, created_at DESC)")


def downgrade() -> None:
    for table in ("alerts", "maintenance_events", "processes",
                  "device_tokens", "devices", "users"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
