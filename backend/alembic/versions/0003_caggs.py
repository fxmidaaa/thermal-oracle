"""Continuous aggregates: rollup 1 мин (180 дн) → 1 час (2 года).

См. architecture.md §4.2. CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)
не выполняется внутри транзакции — поэтому autocommit_block.
"""
from alembic import op

revision = "0003_caggs"
down_revision = "0002_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # materialized_only=false: свежий хвост (ещё не материализованный)
        # дочитывается из raw на лету — дашборд не отстаёт на refresh-лаг.
        op.execute("""
            CREATE MATERIALIZED VIEW telemetry_1m
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT device_id,
                   time_bucket(INTERVAL '1 minute', ts) AS bucket,
                   avg(cpu_temp)  AS cpu_temp_avg,  max(cpu_temp)  AS cpu_temp_max,
                   avg(gpu_temp)  AS gpu_temp_avg,  max(gpu_temp)  AS gpu_temp_max,
                   avg(cpu_power) AS cpu_power_avg, max(cpu_power) AS cpu_power_max,
                   avg(gpu_power) AS gpu_power_avg, max(gpu_power) AS gpu_power_max,
                   avg(fan_rpm)   AS fan_rpm_avg,   max(fan_rpm)   AS fan_rpm_max,
                   count(*)       AS n
            FROM telemetry_raw
            GROUP BY device_id, bucket
            WITH NO DATA
        """)
        op.execute("""
            SELECT add_continuous_aggregate_policy('telemetry_1m',
                start_offset      => INTERVAL '1 hour',
                end_offset        => INTERVAL '2 minutes',
                schedule_interval => INTERVAL '1 minute')
        """)
        op.execute("SELECT add_retention_policy('telemetry_1m', drop_after => INTERVAL '180 days')")
        op.execute("ALTER MATERIALIZED VIEW telemetry_1m SET (timescaledb.compress = true)")
        op.execute("""
            SELECT add_compression_policy('telemetry_1m', compress_after => INTERVAL '7 days')
        """)

        # Иерархический CAgg поверх telemetry_1m. avg(avg) слегка смещён при
        # неполных минутах — для отображения многолетних трендов это приемлемо
        # (аналитика Rth работает по raw, не по rollup'ам).
        op.execute("""
            CREATE MATERIALIZED VIEW telemetry_1h
            WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
            SELECT device_id,
                   time_bucket(INTERVAL '1 hour', bucket) AS bucket,
                   avg(cpu_temp_avg)  AS cpu_temp_avg,  max(cpu_temp_max)  AS cpu_temp_max,
                   avg(gpu_temp_avg)  AS gpu_temp_avg,  max(gpu_temp_max)  AS gpu_temp_max,
                   avg(cpu_power_avg) AS cpu_power_avg, max(cpu_power_max) AS cpu_power_max,
                   avg(gpu_power_avg) AS gpu_power_avg, max(gpu_power_max) AS gpu_power_max,
                   avg(fan_rpm_avg)   AS fan_rpm_avg,   max(fan_rpm_max)   AS fan_rpm_max,
                   sum(n)             AS n
            FROM telemetry_1m
            GROUP BY device_id, time_bucket(INTERVAL '1 hour', bucket)
            WITH NO DATA
        """)
        op.execute("""
            SELECT add_continuous_aggregate_policy('telemetry_1h',
                start_offset      => INTERVAL '1 day',
                end_offset        => INTERVAL '1 hour',
                schedule_interval => INTERVAL '30 minutes')
        """)
        op.execute("SELECT add_retention_policy('telemetry_1h', drop_after => INTERVAL '2 years')")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS telemetry_1h CASCADE")
        op.execute("DROP MATERIALIZED VIEW IF EXISTS telemetry_1m CASCADE")
