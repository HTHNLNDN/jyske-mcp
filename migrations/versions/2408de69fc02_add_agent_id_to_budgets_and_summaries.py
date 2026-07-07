"""add_agent_id_to_budgets_and_summaries

Revision ID: 2408de69fc02
Revises: 64bed3498587
Create Date: 2026-06-30 20:36:34.943875

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2408de69fc02'
down_revision: Union[str, Sequence[str], None] = '64bed3498587'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('budgets', sa.Column('agent_id', sa.Text(), nullable=False, server_default='finance'))
    op.add_column('session_summaries', sa.Column('agent_id', sa.Text(), nullable=False, server_default='finance'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('budgets', 'agent_id')
    op.drop_column('session_summaries', 'agent_id')
