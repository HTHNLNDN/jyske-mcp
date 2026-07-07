"""add_tips_table

Revision ID: a3f153be37cc
Revises: 784418892304
Create Date: 2026-07-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f153be37cc'
down_revision: Union[str, Sequence[str], None] = '784418892304'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE tips (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id              TEXT NOT NULL DEFAULT 'finance',
            created_at            REAL NOT NULL,
            tip_date              TEXT NOT NULL,
            window_from           TEXT NOT NULL,
            window_to             TEXT NOT NULL,
            tip_text              TEXT NOT NULL,
            subject_key           TEXT,
            category_top          TEXT,
            based_on              TEXT,
            signals_json          TEXT NOT NULL,
            model                 TEXT NOT NULL,
            prompt_version        TEXT NOT NULL,
            feedback_status       TEXT NOT NULL DEFAULT 'pending',
            feedback_reason_code  TEXT,
            feedback_reason_text  TEXT,
            feedback_source       TEXT,
            feedback_at           REAL,
            UNIQUE(agent_id, tip_date)
        )
    """)
    op.execute("CREATE INDEX ix_tips_tip_date ON tips(tip_date)")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX ix_tips_tip_date")
    op.execute("DROP TABLE tips")
