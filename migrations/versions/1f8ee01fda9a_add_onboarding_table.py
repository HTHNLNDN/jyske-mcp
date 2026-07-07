"""add_onboarding_table

Revision ID: 1f8ee01fda9a
Revises: aa1665106662
Create Date: 2026-06-30 20:36:35.336272

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1f8ee01fda9a'
down_revision: Union[str, Sequence[str], None] = 'aa1665106662'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE onboarding (
            agent_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL DEFAULT 'income',
            income REAL,
            income_day INTEGER,
            fixed_costs TEXT,
            savings_monthly REAL,
            savings_purpose TEXT,
            savings_target REAL,
            savings_deadline TEXT,
            budget_style TEXT DEFAULT 'honest',
            completed_at REAL,
            updated_at REAL NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE budget_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL DEFAULT 'finance',
            category_top TEXT NOT NULL,
            period TEXT NOT NULL,
            limit_amount REAL NOT NULL,
            actual_amount REAL NOT NULL,
            variance REAL NOT NULL,
            created_at REAL NOT NULL
        )
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE budget_history")
    op.execute("DROP TABLE onboarding")
