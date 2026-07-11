"""
Unit tests for Storage.sum_spending (jyske_mcp/slices/finance/storage.py) — the core
aggregation query underlying get_spending/compare_spending/get_budget_status.
Groups debit spending (direction != 'CRDT') between two ISO dates by the
requested key, always folded per currency too (see sum_spending's docstring
-- never blend currencies).

Uses a real temporary SQLite DB with the transactions DDL from
migrations/versions/64bed3498587_initial_schema.py plus the `direction`
column added by 784418892304_add_math_aggregation_tools.py -- Storage no
longer creates tables itself, so the fixture must (same pattern as
tests/test_storage_synthetic_id.py / tests/test_mixed_currency_no_blend.py).
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


@pytest.fixture
def storage(monkeypatch, tmp_path):
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_TRANSACTIONS_DDL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage_module, "_CACHE_DB", db_path)
    # Avoid touching ~/.config/mcp-bank in _db()'s CONFIG_DIR.mkdir/chmod.
    monkeypatch.setattr(storage_module, "CONFIG_DIR", tmp_path)

    return Storage()


_counter = 0


def _insert_tx(storage, *, category_top, category_mid, amount, currency, day,
                direction="DBIT", account_uid="acc1"):
    global _counter
    _counter += 1
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO transactions "
        "(account_uid, transaction_id, date, amount, currency, description, "
        " category_top, category_mid, created_at, direction) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_uid,
            f"tx-{_counter}",
            day,
            amount,
            currency,
            f"{category_mid or category_top} purchase",
            category_top,
            category_mid,
            time.time(),
            direction,
        ),
    )
    conn.commit()
    conn.close()


def test_empty_transactions_returns_empty_list(storage):
    rows = storage.sum_spending("2026-07-01", "2026-07-31")
    assert rows == []


def test_groups_by_category_and_currency_separately(storage):
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=100.0, currency="DKK", day="2026-07-05")
    _insert_tx(storage, category_top="Food & Dining", category_mid="Groceries",
               amount=200.0, currency="DKK", day="2026-07-06")
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=30.0, currency="EUR", day="2026-07-07")

    rows = storage.sum_spending("2026-07-01", "2026-07-31", group_by="category")

    # DKK and EUR under the same category must be reported as two separate
    # rows, never folded into one blended amount.
    by_currency = {r["currency"]: r for r in rows}
    assert set(by_currency) == {"DKK", "EUR"}
    assert by_currency["DKK"]["key"] == "Food & Dining"
    assert by_currency["DKK"]["amount"] == 300.0
    assert by_currency["DKK"]["count"] == 2
    assert by_currency["EUR"]["amount"] == 30.0
    assert by_currency["EUR"]["count"] == 1


def test_multiple_categories_are_kept_separate(storage):
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=100.0, currency="DKK", day="2026-07-05")
    _insert_tx(storage, category_top="Transport", category_mid="Fuel",
               amount=250.0, currency="DKK", day="2026-07-06")

    rows = storage.sum_spending("2026-07-01", "2026-07-31", group_by="category")
    by_key = {r["key"]: r for r in rows}

    assert set(by_key) == {"Food & Dining", "Transport"}
    assert by_key["Food & Dining"]["amount"] == 100.0
    assert by_key["Transport"]["amount"] == 250.0


def test_group_by_mid_narrows_within_a_top_category(storage):
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=100.0, currency="DKK", day="2026-07-05")
    _insert_tx(storage, category_top="Food & Dining", category_mid="Groceries",
               amount=200.0, currency="DKK", day="2026-07-06")

    rows = storage.sum_spending(
        "2026-07-01", "2026-07-31", category_top="Food & Dining", group_by="mid"
    )
    by_key = {r["key"]: r["amount"] for r in rows}

    assert by_key == {"Restaurants": 100.0, "Groceries": 200.0}


def test_credit_direction_is_excluded(storage):
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=100.0, currency="DKK", day="2026-07-05", direction="DBIT")
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=5000.0, currency="DKK", day="2026-07-06", direction="CRDT")

    rows = storage.sum_spending("2026-07-01", "2026-07-31", group_by="category")

    # A salary/refund credit must never be summed into spending.
    assert len(rows) == 1
    assert rows[0]["amount"] == 100.0


def test_date_range_is_exclusive_of_transactions_outside_bounds(storage):
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=100.0, currency="DKK", day="2026-06-30")  # just before window
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=200.0, currency="DKK", day="2026-07-15")  # inside window
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=300.0, currency="DKK", day="2026-08-01")  # just after window

    rows = storage.sum_spending("2026-07-01", "2026-07-31", group_by="category")

    assert len(rows) == 1
    assert rows[0]["amount"] == 200.0


def test_invalid_group_by_raises_value_error(storage):
    with pytest.raises(ValueError):
        storage.sum_spending("2026-07-01", "2026-07-31", group_by="not-a-real-group")
