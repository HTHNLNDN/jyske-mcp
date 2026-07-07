"""add_math_aggregation_tools

Revision ID: 784418892304
Revises: 1f8ee01fda9a
Create Date: 2026-07-04 20:31:34.812419

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '784418892304'
down_revision: Union[str, Sequence[str], None] = '1f8ee01fda9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('transactions', sa.Column('direction', sa.Text(), nullable=True))

    # Backfill direction from the raw Enable Banking payload for existing rows.
    op.execute(
        "UPDATE transactions "
        "SET direction = json_extract(raw_data, '$.credit_debit_indicator')"
    )

    # Backfill category columns from the merchants cache for any rows that
    # were stored before a merchant had been categorized (store_transaction
    # only writes category_* when categorize() already has an answer at
    # insert time — see lib/storage.py).
    op.execute("""
        UPDATE transactions
        SET category_top = (
                SELECT m.category_top FROM merchants m
                WHERE m.raw_name = transactions.description
            ),
            category_mid = (
                SELECT m.category_mid FROM merchants m
                WHERE m.raw_name = transactions.description
            ),
            category_leaf = (
                SELECT m.category_leaf FROM merchants m
                WHERE m.raw_name = transactions.description
            )
        WHERE category_top IS NULL
          AND EXISTS (
                SELECT 1 FROM merchants m WHERE m.raw_name = transactions.description
              )
    """)

    op.execute("CREATE INDEX ix_transactions_date ON transactions(date)")

    op.execute("""
        CREATE TABLE recurring_charge_status (
            merchant     TEXT NOT NULL,
            currency     TEXT NOT NULL,
            status       TEXT NOT NULL,
            confirmed_at REAL NOT NULL,
            PRIMARY KEY (merchant, currency)
        )
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE recurring_charge_status")
    op.execute("DROP INDEX ix_transactions_date")
    # SQLite (3.35+) supports plain DROP COLUMN for simple columns with no
    # constraints/generated-column dependents, same as this project's other
    # add_column/drop_column migrations (see
    # 2408de69fc02_add_agent_id_to_budgets_and_summaries.py) — no batch mode
    # needed here.
    op.drop_column('transactions', 'direction')
