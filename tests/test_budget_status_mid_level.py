"""
Covers the mid-level budget scoring bug in jyske_mcp/slices/finance/storage.py's
get_budget_status().

Previously, spending was aggregated with sum_spending(..., group_by="category")
(grouping by category_top only) and every budget row — including ones with
category_mid set — looked up `spent` from that top-level-only map. A
mid-level budget (e.g. Food & Dining > Restaurants) was therefore scored
against ALL Food & Dining spend, not just Restaurants spend.

get_budget_status() now branches per budget row: rows with category_mid set
query sum_spending(group_by="mid", category_top=<row's top>) and pick out
just that mid category's spend, while top-level-only rows keep using the
single top-level aggregate query (no extra query in the common case).

Uses a real temporary SQLite DB with the same DDL as
migrations/versions/64bed3498587_initial_schema.py plus the agent_id column
added by 2408de69fc02_add_agent_id_to_budgets_and_summaries.py — Storage no
longer creates tables itself (see jyske_mcp/kernel/storage.py's
_check_schema_version), so the fixture must create them. Transactions are
inserted directly via raw SQL (not store_transaction/categorize) so
category_top/category_mid can be set explicitly and deterministically.
"""
import sqlite3
import time

import pytest

import jyske_mcp.kernel.storage as storage_module
from jyske_mcp.slices.finance.storage import Storage

_TRANSACTIONS_DDL = """
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
        created_at     REAL NOT NULL,
        direction      TEXT
    )
"""

_BUDGETS_DDL = """
    CREATE TABLE budgets (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        category_top TEXT NOT NULL,
        category_mid TEXT,
        limit_amount REAL NOT NULL,
        period       TEXT NOT NULL DEFAULT 'monthly',
        active       INTEGER NOT NULL DEFAULT 1,
        created_at   REAL NOT NULL,
        agent_id     TEXT NOT NULL DEFAULT 'finance'
    )
"""


@pytest.fixture
def storage(monkeypatch, tmp_path):
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_TRANSACTIONS_DDL)
    conn.execute(_BUDGETS_DDL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage_module, "_CACHE_DB", db_path)
    # Avoid touching ~/.config/mcp-bank in _db()'s CONFIG_DIR.mkdir/chmod.
    monkeypatch.setattr(storage_module, "CONFIG_DIR", tmp_path)

    return Storage()


def _insert_tx(storage, *, category_top, category_mid, amount, day):
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO transactions "
        "(account_uid, transaction_id, date, amount, currency, description, "
        " category_top, category_mid, created_at, direction) "
        "VALUES (?, ?, ?, ?, 'DKK', ?, ?, ?, ?, 'DBIT')",
        (
            "acc1",
            f"tx-{category_mid}-{day}-{amount}",
            day,
            amount,
            f"{category_mid or category_top} purchase",
            category_top,
            category_mid,
            time.time(),
        ),
    )
    conn.commit()
    conn.close()


def _this_month_day(storage, day: int = 5) -> str:
    month_start, _today = storage.current_month_window()
    return f"{month_start[:7]}-{day:02d}"


def test_mid_level_budget_scores_only_its_own_mid_category(storage):
    day = _this_month_day(storage)

    # Two different mid categories under the same top category.
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants", amount=200.0, day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants", amount=150.0, day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Groceries", amount=300.0, day=day)

    storage.set_budget(category_top="Food & Dining", category_mid="Restaurants", limit_amount=800.0, period="monthly")

    statuses = storage.get_budget_status()
    assert len(statuses) == 1
    status = statuses[0]
    assert status["category"] == "Food & Dining"
    assert status["category_mid"] == "Restaurants"
    # Only Restaurants spend (200 + 150 = 350), NOT the full Food & Dining
    # total (200 + 150 + 300 = 650).
    assert status["spent"] == 350.0


def test_top_level_budget_still_aggregates_full_top_category(storage):
    day = _this_month_day(storage)

    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants", amount=200.0, day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants", amount=150.0, day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Groceries", amount=300.0, day=day)

    storage.set_budget(category_top="Food & Dining", limit_amount=1000.0, period="monthly")

    statuses = storage.get_budget_status()
    assert len(statuses) == 1
    status = statuses[0]
    assert status["category"] == "Food & Dining"
    assert status["category_mid"] is None
    assert status["spent"] == 650.0
