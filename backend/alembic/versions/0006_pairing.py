"""Pairing-коды: короткоживущее одноразовое сопряжение агента с аккаунтом.

Хранится только SHA-256 кода; одноразовость обеспечивается атомарным
UPDATE ... WHERE used_at IS NULL (см. services/pairing_service.py).
"""
from alembic import op

revision = "0006_pairing"
down_revision = "0005_analytics_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE pairing_codes (
            code_hash  bytea PRIMARY KEY,
            user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at    timestamptz,
            device_id  uuid REFERENCES devices(id) ON DELETE SET NULL
        )
    """)
    op.execute("CREATE INDEX ix_pairing_codes_user ON pairing_codes (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pairing_codes")
