"""
Covers the NULL-transaction_id dedup fix in jyske_mcp/kernel/storage.py.

transaction_id is TEXT UNIQUE, but SQLite treats every NULL as distinct
from every other NULL, so store_transaction()'s
ON CONFLICT(transaction_id) DO UPDATE never fired for transactions lacking
both transaction_id and entry_reference — they re-inserted (duplicated) on
every overlapping sync. store_transaction() now assigns these rows a
deterministic synthetic id (jyske_mcp.kernel.storage.synthetic_transaction_id)
so the upsert works correctly.

Uses a real temporary SQLite DB with the same DDL as
migrations/versions/64bed3498587_initial_schema.py plus the columns added
by 784418892304_add_math_aggregation_tools.py (direction) — Storage no
longer creates tables itself (see jyske_mcp/kernel/storage.py's
_check_schema_version), so the fixture must create them.
"""
import sqlite3
import tempfile
from pathlib import Path

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

_MERCHANTS_DDL = """
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
"""


@pytest.fixture
def storage(monkeypatch, tmp_path):
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_TRANSACTIONS_DDL)
    conn.execute(_MERCHANTS_DDL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage_module, "_CACHE_DB", db_path)
    # Avoid touching ~/.config/mcp-bank in _db()'s CONFIG_DIR.mkdir/chmod.
    monkeypatch.setattr(storage_module, "CONFIG_DIR", tmp_path)

    # categorize() hits the merchants cache / MCC lookup — with an empty
    # merchants table and no mcc these txs categorize to None, which is
    # fine, we're not asserting on category columns here.

    return Storage()


def _all_rows(storage):
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    rows = conn.execute(
        "SELECT transaction_id, amount, description FROM transactions"
    ).fetchall()
    conn.close()
    return rows


def test_double_store_null_id_dedups_to_one_row(storage):
    tx = {
        "transaction_id": None,
        "entry_reference": None,
        "booking_date": "2026-07-01",
        "transaction_amount": {"amount": "12.50", "currency": "DKK"},
        "creditor_name": "Some Shop",
    }

    storage.store_transaction("acc1", tx)
    storage.store_transaction("acc1", tx)

    rows = _all_rows(storage)
    assert len(rows) == 1
    assert rows[0][0].startswith("synth:")


def test_double_store_real_id_still_upserts_unprefixed(storage):
    tx = {
        "transaction_id": "realbank123",
        "booking_date": "2026-07-01",
        "transaction_amount": {"amount": "9.99", "currency": "DKK"},
        "creditor_name": "Real Shop",
    }

    storage.store_transaction("acc1", tx)
    storage.store_transaction("acc1", tx)

    rows = _all_rows(storage)
    assert len(rows) == 1
    assert rows[0][0] == "realbank123"


def test_distinct_null_id_txs_do_not_collapse(storage):
    tx_a = {
        "transaction_id": None,
        "entry_reference": None,
        "booking_date": "2026-07-01",
        "transaction_amount": {"amount": "10.00", "currency": "DKK"},
        "creditor_name": "Shop A",
    }
    tx_b = {
        "transaction_id": None,
        "entry_reference": None,
        "booking_date": "2026-07-01",
        "transaction_amount": {"amount": "20.00", "currency": "DKK"},
        "creditor_name": "Shop A",
    }

    storage.store_transaction("acc1", tx_a)
    storage.store_transaction("acc1", tx_b)

    rows = _all_rows(storage)
    assert len(rows) == 2
    ids = {row[0] for row in rows}
    assert len(ids) == 2
    assert all(tid.startswith("synth:") for tid in ids)
