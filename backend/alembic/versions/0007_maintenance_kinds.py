"""+ вид обслуживания 'repad' (замена термопрокладок) — Maintenance API Шага 6.

Postgres именует inline-CHECK колонки как <table>_<column>_check.
"""
from alembic import op

revision = "0007_maintenance_kinds"
down_revision = "0006_pairing"
branch_labels = None
depends_on = None

KINDS_NEW = "('repaste','cleaning','repad','fan_curve_change','undervolt_change','hw_change')"
KINDS_OLD = "('repaste','cleaning','fan_curve_change','undervolt_change','hw_change')"


def upgrade() -> None:
    op.execute("ALTER TABLE maintenance_events DROP CONSTRAINT maintenance_events_kind_check")
    op.execute(
        "ALTER TABLE maintenance_events ADD CONSTRAINT maintenance_events_kind_check "
        f"CHECK (kind IN {KINDS_NEW})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM maintenance_events WHERE kind = 'repad'")
    op.execute("ALTER TABLE maintenance_events DROP CONSTRAINT maintenance_events_kind_check")
    op.execute(
        "ALTER TABLE maintenance_events ADD CONSTRAINT maintenance_events_kind_check "
        f"CHECK (kind IN {KINDS_OLD})"
    )
