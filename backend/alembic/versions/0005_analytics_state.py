"""Watermark инкрементальной детекции окон + диагноз в device_health.

analytics_state: окна эмитятся только закрытые; каждый прогон перечитывает
LOOKBACK (= max окна + грейс) назад от watermark — upsert делает повторную
обработку идемпотентной, состояние state machine хранить не нужно.
"""
from alembic import op

revision = "0005_analytics_state"
down_revision = "0004_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE analytics_state (
            device_id uuid PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
            windows_processed_until timestamptz
        )
    """)
    op.execute("""
        ALTER TABLE device_health
            ADD COLUMN diagnosis text NOT NULL DEFAULT 'insufficient_data'
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE device_health DROP COLUMN diagnosis")
    op.execute("DROP TABLE IF EXISTS analytics_state")
