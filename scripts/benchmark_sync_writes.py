"""
Standalone benchmark for the per-account batched-insert fix in
jyske_mcp/kernel/storage.py (Storage.store_transaction / store_transactions_batch).

NOT a pytest test — run directly:

    python scripts/benchmark_sync_writes.py [-n COUNT]

Never touches the real cache.db and never calls Enable Banking. Builds a
throwaway SQLite DB under a fresh tempfile.mkdtemp() directory, using the
same transactions/merchants DDL as tests/test_storage_synthetic_id.py's
fixture (which itself mirrors migrations/versions/64bed3498587_initial_schema.py
plus the `direction` column added by
784418892304_add_math_aggregation_tools.py) — Storage no longer creates
tables itself.

Method A: the old per-row pattern — one store_transaction() call per
transaction, each opening/committing/closing its own connection.
Method B: store_transactions_batch() — one connection, one commit for the
whole account.

Both methods write to their own freshly-seeded copy of the temp DB (same
schema, disjoint transaction_ids) so timings aren't skewed by one method
warming the page cache for the other.
"""
import argparse
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import jyske_mcp.kernel.config as config_module
# _db() is defined in kernel/storage.py, and re-reads its OWN module's
# _CACHE_DB/CONFIG_DIR globals at call time regardless of which subclass
# instance calls it — so those two globals must be monkeypatched on
# jyske_mcp.kernel.storage, not on jyske_mcp.slices.finance.storage (see
# .agent/epics/vsa-restructure-blueprint.md §2 and
# jyske_mcp.kernel.storage.KernelStorage._db's docstring).
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


def _make_temp_db(tmp_dir: Path, name: str) -> Path:
    db_path = tmp_dir / name
    assert db_path != config_module.DB_FILE, (
        "refusing to benchmark against the real cache.db"
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(_TRANSACTIONS_DDL)
    conn.execute(_MERCHANTS_DDL)
    conn.commit()
    conn.close()
    return db_path


def _synthetic_txs(n: int, prefix: str) -> list[dict]:
    creditors = ["Netto", "Coop 365", "Rema 1000", "Netflix", "Spotify", "Shell"]
    txs = []
    for i in range(n):
        day = 1 + (i % 28)
        month = 1 + ((i // 28) % 12)
        txs.append({
            "transaction_id": f"{prefix}-{i}",
            "booking_date": f"2026-{month:02d}-{day:02d}",
            "transaction_amount": {
                "amount": str(round(10 + (i % 500) * 1.37, 2)),
                "currency": "DKK",
            },
            "creditor_name": creditors[i % len(creditors)],
            "credit_debit_indicator": "DBIT",
        })
    return txs


class _ConnCounter:
    """Wraps Storage._db to count how many connections get opened."""

    def __init__(self, storage: Storage):
        self.count = 0
        self._orig = storage._db

        def counted():
            self.count += 1
            return self._orig()

        storage._db = counted


def _bench_per_row(db_path: Path, txs: list[dict]) -> tuple[float, int]:
    storage_module._CACHE_DB = db_path
    storage = Storage()
    counter = _ConnCounter(storage)

    start = time.perf_counter()
    for tx in txs:
        storage.store_transaction("acc-bench", tx)
    elapsed = time.perf_counter() - start
    return elapsed, counter.count


def _bench_batch(db_path: Path, txs: list[dict]) -> tuple[float, int]:
    storage_module._CACHE_DB = db_path
    storage = Storage()
    counter = _ConnCounter(storage)

    start = time.perf_counter()
    storage.store_transactions_batch("acc-bench", txs)
    elapsed = time.perf_counter() - start
    return elapsed, counter.count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--count", type=int, default=5000,
                         help="number of synthetic transactions per method (default: 5000)")
    args = parser.parse_args()

    tmp_dir = Path(tempfile.mkdtemp(prefix="jyske-mcp-bench-"))
    assert tmp_dir != config_module.CONFIG_DIR, "refusing to benchmark against the real config dir"

    # Monkeypatch both module-level globals the same way
    # tests/test_storage_synthetic_id.py's fixture does, so Storage()
    # writes to our throwaway files instead of ~/.config/mcp-bank.
    orig_cache_db = storage_module._CACHE_DB
    orig_config_dir = storage_module.CONFIG_DIR
    storage_module.CONFIG_DIR = tmp_dir

    try:
        db_a = _make_temp_db(tmp_dir, "bench_per_row.db")
        db_b = _make_temp_db(tmp_dir, "bench_batch.db")

        txs = _synthetic_txs(args.count, "perrow")
        time_a, opens_a = _bench_per_row(db_a, txs)

        txs_b = _synthetic_txs(args.count, "batch")
        time_b, opens_b = _bench_batch(db_b, txs_b)

        print(f"\nBenchmark: storing {args.count} synthetic transactions for one account\n")
        print(f"{'method':<28}{'wall-clock (s)':<18}{'connections opened':<20}")
        print("-" * 66)
        print(f"{'A: per-row store_transaction':<28}{time_a:<18.3f}{opens_a:<20}")
        print(f"{'B: store_transactions_batch':<28}{time_b:<18.3f}{opens_b:<20}")
        print()
        print(f"Connections opened collapse: O(N)={opens_a} -> O(1)={opens_b}")
        if time_b > 0:
            print(f"Speedup: {time_a / time_b:.1f}x")
    finally:
        storage_module._CACHE_DB = orig_cache_db
        storage_module.CONFIG_DIR = orig_config_dir
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
