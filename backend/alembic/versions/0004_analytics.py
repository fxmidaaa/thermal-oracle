"""Производные таблицы аналитики: ambient, окна Rth, снапшот здоровья.

Схема по architecture.md §4.5; наполняет их analytics worker (следующие шаги).
Создаём сейчас, чтобы схема БД была полной и зафиксированной.
"""
from alembic import op

revision = "0004_analytics"
down_revision = "0003_caggs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ambient_estimates (
            device_id     uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            day           date NOT NULL,
            t_ambient     real NOT NULL,
            confidence    real NOT NULL,
            idle_minutes  integer NOT NULL,
            episodes_n    integer NOT NULL,
            method        text NOT NULL DEFAULT 'idle_p5_v1',
            model_version integer NOT NULL,
            PRIMARY KEY (device_id, day)
        )
    """)

    op.execute("""
        CREATE TABLE rth_windows (
            id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            device_id           uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            domain              text NOT NULL CHECK (domain IN ('cpu', 'gpu', 'package')),
            window_start        timestamptz NOT NULL,
            window_end          timestamptz NOT NULL,
            duration_s          integer NOT NULL,
            p_tail              real NOT NULL,
            p_cv                real NOT NULL,
            t_tail              real NOT NULL,
            dtdt_tail           real NOT NULL,
            fan_rpm_avg         real,
            t_ambient           real NOT NULL,
            ambient_confidence  real NOT NULL,
            rth                 real NOT NULL,
            stratum             text NOT NULL,
            dominant_process_id integer REFERENCES processes(id),
            quality             real NOT NULL,
            model_version       integer NOT NULL,
            UNIQUE (device_id, domain, window_start)
        )
    """)
    op.execute("""
        CREATE INDEX ix_rth_windows_device_time
            ON rth_windows (device_id, window_start DESC)
    """)

    op.execute("""
        CREATE TABLE device_health (
            device_id              uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            domain                 text NOT NULL CHECK (domain IN ('cpu', 'gpu', 'package')),
            computed_at            timestamptz NOT NULL,
            epoch_start            timestamptz,
            rth_baseline           real,
            rth_current            real,
            degradation_pct        real,
            slope_mkw_per_30d      real,
            slope_ci_low           real,
            slope_ci_high          real,
            forecast_throttle_date date,
            health_score           smallint,
            data_quality           text NOT NULL DEFAULT 'sparse',
            model_version          integer NOT NULL,
            PRIMARY KEY (device_id, domain)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS device_health")
    op.execute("DROP TABLE IF EXISTS rth_windows")
    op.execute("DROP TABLE IF EXISTS ambient_estimates")
