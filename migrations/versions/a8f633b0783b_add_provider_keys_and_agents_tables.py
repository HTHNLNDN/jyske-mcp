"""add_provider_keys_and_agents_tables

Revision ID: a8f633b0783b
Revises: a3f153be37cc
Create Date: 2026-07-05 21:18:27.955276

"""
import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8f633b0783b'
down_revision: Union[str, Sequence[str], None] = 'a3f153be37cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE provider_keys (
            provider   TEXT PRIMARY KEY,
            api_key    TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE agents (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT,
            model       TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        )
    """)
    _seed_finance_agent()


def _seed_finance_agent() -> None:
    """Seed the one agent that previously lived as a static AGENTS list in
    app.py. model is deliberately left NULL — model/key selection is now a
    user-driven DB-configured step (see Settings > Model & keys), not a
    baked-in default."""
    conn = op.get_bind()
    now = time.time()
    conn.exec_driver_sql(
        "INSERT INTO agents (id, name, description, model, created_at, updated_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        ("finance", "Finance Agent", "Personal finance companion", now, now),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE agents")
    op.execute("DROP TABLE provider_keys")
