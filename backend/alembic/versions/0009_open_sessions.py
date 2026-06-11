"""analytics_state.open_since — начала сессий, открытых на правом крае.

Уточнение к решению 0005 («состояние state machine хранить не нужно»):
для сессий КОРОЧЕ lookback это верно, но живые данные показали два артефакта
длинных/незавершённых сессий: (1) обрезанный правым краем прогона хвост
эмитился как закрытое окно — CV на огрызке проходил гейт, рождая фантомные
Rth-точки; (2) следующий прогон, чей lookback попадал в середину сессии,
обрезал её слева и эмитил тот же интервал под другим window_start — дубли.

Фикс двухчастный: открытые на краю окна не эмитятся (windows.py), а начало
открытой сессии хранится здесь — {"cpu": epoch_float, ...} — и следующий
прогон перечитывает данные от него, воспроизводя чанки с теми же ключами.
Потеря состояния не фатальна: поведение деградирует до прежнего lookback.
"""
from alembic import op

revision = "0009_open_sessions"
down_revision = "0008_regime_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE analytics_state ADD COLUMN open_since jsonb NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE analytics_state DROP COLUMN open_since")
