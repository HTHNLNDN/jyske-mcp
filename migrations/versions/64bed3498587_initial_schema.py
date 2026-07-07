"""initial_schema

Revision ID: 64bed3498587
Revises: 
Create Date: 2026-06-30 20:27:45.739535

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '64bed3498587'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE cache (
            key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            cached_at REAL NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE merchants (
            raw_name      TEXT PRIMARY KEY,
            category_top  TEXT,
            category_mid  TEXT,
            category_leaf TEXT,
            resolved_name TEXT,
            mcc           TEXT,
            source        TEXT,
            created_at    REAL
        )
    """)
    op.execute("""
        CREATE TABLE session_summaries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            summary    TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE user_profile (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE syncs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at           REAL,
            completed_at         REAL,
            accounts_synced      INTEGER,
            transactions_fetched INTEGER,
            new_transactions     INTEGER,
            errors               TEXT
        )
    """)
    op.execute("""
        CREATE TABLE transactions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            account_uid    TEXT NOT NULL,
            transaction_id TEXT UNIQUE,
            date           TEXT NOT NULL,
            amount         REAL,
            currency       TEXT,
            description    TEXT,
            mcc            TEXT,
            category_top   TEXT,
            category_mid   TEXT,
            category_leaf  TEXT,
            raw_data       TEXT,
            created_at     REAL NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE balances (
            account_uid TEXT PRIMARY KEY,
            data        TEXT NOT NULL,
            fetched_at  REAL NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE budgets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            category_top TEXT NOT NULL,
            category_mid TEXT,
            limit_amount REAL NOT NULL,
            period       TEXT NOT NULL DEFAULT 'monthly',
            active       INTEGER NOT NULL DEFAULT 1,
            created_at   REAL NOT NULL
        )
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE budgets")
    op.execute("DROP TABLE balances")
    op.execute("DROP TABLE transactions")
    op.execute("DROP TABLE syncs")
    op.execute("DROP TABLE user_profile")
    op.execute("DROP TABLE session_summaries")
    op.execute("DROP TABLE merchants")
    op.execute("DROP TABLE cache")
