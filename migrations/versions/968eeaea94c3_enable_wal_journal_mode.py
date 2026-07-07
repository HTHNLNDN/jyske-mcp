"""enable wal journal mode

Revision ID: 968eeaea94c3
Revises: a8f633b0783b
Create Date: 2026-07-07 17:47:06.908179

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '968eeaea94c3'
down_revision: Union[str, Sequence[str], None] = 'a8f633b0783b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _set_journal_mode(mode: str) -> None:
    """PRAGMA journal_mode is a no-op when run inside a transaction, and
    Alembic always wraps migrations in one — so this drops to the raw
    pysqlite DBAPI connection and forces autocommit instead of using
    execution_options(isolation_level="AUTOCOMMIT") (that raises in
    SQLAlchemy 2.0 once a transaction is already open).

    journal_mode=WAL is persisted in the database file header, not
    per-connection, so this is a one-time migration rather than something
    _db() needs to set on every connect — lib/storage.py's _db() is
    intentionally left untouched.
    """
    bind = op.get_bind()
    raw = bind.connection.dbapi_connection  # SQLAlchemy >=1.4.24/2.0
    prev = raw.isolation_level
    raw.isolation_level = None  # autocommit, no implicit BEGIN
    try:
        if raw.in_transaction:
            raw.commit()  # end Alembic's empty transaction
        got = raw.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
    finally:
        raw.isolation_level = prev
    if got is None or got.lower() != mode.lower():
        raise RuntimeError(
            f"journal_mode switch failed: wanted {mode!r}, DB reports {got!r}. "
            "Stop app.py and cron/scheduler.py, then retry."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _set_journal_mode("WAL")


def downgrade() -> None:
    """Downgrade schema."""
    _set_journal_mode("DELETE")
