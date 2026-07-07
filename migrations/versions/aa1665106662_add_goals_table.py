"""add_goals_table

Revision ID: aa1665106662
Revises: 2408de69fc02
Create Date: 2026-06-30 20:36:35.141154

"""
import json
import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'aa1665106662'
down_revision: Union[str, Sequence[str], None] = '2408de69fc02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL DEFAULT 'finance',
            name TEXT NOT NULL,
            target_amount REAL,
            current_amount REAL DEFAULT 0,
            purpose TEXT,
            deadline TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    _migrate_goals_out_of_user_profile()


def _migrate_goals_out_of_user_profile() -> None:
    """One-time data migration: goals used to live as a single freeform JSON
    blob in user_profile (key='goals'), written ad hoc by the LLM via
    update_memory. Now that there's a dedicated table, move whatever is
    there into proper rows and drop the blob."""
    conn = op.get_bind()
    row = conn.exec_driver_sql(
        "SELECT value FROM user_profile WHERE key = 'goals'"
    ).fetchone()

    if row is not None and row[0]:
        try:
            goals = json.loads(row[0])
        except (TypeError, ValueError):
            goals = None

        if isinstance(goals, dict):
            goals = [goals]

        if isinstance(goals, list):
            now = time.time()
            for g in goals:
                if isinstance(g, str):
                    g = {"name": g}
                if not isinstance(g, dict):
                    continue
                name = g.get("name") or g.get("title") or g.get("goal") or "Goal"
                target_amount = g.get("target_amount") or g.get("target") or g.get("amount")
                current_amount = (
                    g.get("current_amount") or g.get("current") or g.get("progress") or 0
                )
                purpose = g.get("purpose") or g.get("description") or g.get("note")
                deadline = g.get("deadline") or g.get("date") or g.get("target_date")
                conn.exec_driver_sql(
                    "INSERT INTO goals "
                    "(agent_id, name, target_amount, current_amount, purpose, deadline, "
                    " active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    ("finance", name, target_amount, current_amount, purpose, deadline, now, now),
                )

    conn.exec_driver_sql("DELETE FROM user_profile WHERE key = 'goals'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE goals")
