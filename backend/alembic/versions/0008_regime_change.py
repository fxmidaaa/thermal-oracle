"""+ вид обслуживания 'regime_change' — CUSUM-предложение (§5.5).

Строки этого вида создаёт только update_trends (source='changepoint_suggested'):
обнаружена ступенька дневных медиан Rth — смена режима (кривая вентиляторов,
андервольт, чистка), пользователю предлагается подтвердить, что это было.
Через POST /maintenance такой вид завести нельзя (нет в API-enum), и в
EPOCH_RESET_KINDS он не входит — неподтверждённое предложение базлайн не режет.
"""
from alembic import op

revision = "0008_regime_change"
down_revision = "0007_maintenance_kinds"
branch_labels = None
depends_on = None

KINDS_NEW = ("('repaste','cleaning','repad','fan_curve_change',"
             "'undervolt_change','hw_change','regime_change')")
KINDS_OLD = "('repaste','cleaning','repad','fan_curve_change','undervolt_change','hw_change')"


def upgrade() -> None:
    op.execute("ALTER TABLE maintenance_events DROP CONSTRAINT maintenance_events_kind_check")
    op.execute(
        "ALTER TABLE maintenance_events ADD CONSTRAINT maintenance_events_kind_check "
        f"CHECK (kind IN {KINDS_NEW})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM maintenance_events WHERE kind = 'regime_change'")
    op.execute("ALTER TABLE maintenance_events DROP CONSTRAINT maintenance_events_kind_check")
    op.execute(
        "ALTER TABLE maintenance_events ADD CONSTRAINT maintenance_events_kind_check "
        f"CHECK (kind IN {KINDS_OLD})"
    )
