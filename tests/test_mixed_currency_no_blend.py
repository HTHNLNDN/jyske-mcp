"""
Covers the currency de-blending fix across get_spending, compare_spending
(jyske_mcp/mcp/server.py) and get_budget_status (jyske_mcp/slices/finance/storage.py).

Previously each of these summed `amount` across rows regardless of
`currency` — a mixed DKK+EUR transaction set would silently blend into one
number with no exchange rate applied (e.g. 500 DKK + 30 EUR -> "530"). Each
tool now folds per currency instead:
  - get_spending: `total` is a {currency: amount} map.
  - compare_spending: `totals` is keyed by currency.
  - get_budget_status: `spent`/`percent`/`status` are PRIMARY_CURRENCY (DKK)
    only; non-DKK spend is surfaced separately via `other_currency_amounts`.

Uses a real temporary SQLite DB with the same DDL as
migrations/versions/64bed3498587_initial_schema.py plus the columns added by
784418892304_add_math_aggregation_tools.py (direction) and
2408de69fc02_add_agent_id_to_budgets_and_summaries.py (agent_id) — Storage no
longer creates tables itself (see jyske_mcp/kernel/storage.py's
_check_schema_version), so the fixture must create them. Both a fresh
Storage() and jyske_mcp.mcp.server's module-global `storage` read the same
DB, since _db() (defined in jyske_mcp.kernel.storage) re-reads
storage_module._CACHE_DB on every call — monkeypatching that global (and
CONFIG_DIR) is enough to redirect both.
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


def _insert_tx(storage, *, category_top, category_mid, amount, currency, day, direction="DBIT"):
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO transactions "
        "(account_uid, transaction_id, date, amount, currency, description, "
        " category_top, category_mid, created_at, direction) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "acc1",
            f"tx-{category_mid}-{currency}-{day}-{amount}-{time.time()}",
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


def _this_month_day(storage, day: int = 5) -> str:
    month_start, _today = storage.current_month_window()
    return f"{month_start[:7]}-{day:02d}"


def _prev_month_day(storage, day: int = 5) -> str:
    month_start, _today = storage.current_month_window()
    year, mon = int(month_start[:4]), int(month_start[5:7])
    if mon == 1:
        year, mon = year - 1, 12
    else:
        mon -= 1
    return f"{year:04d}-{mon:02d}-{day:02d}"


def assert_no_blend(payload) -> None:
    """Regression guard: walk every numeric value reachable from `payload`
    (dicts/lists of dicts, as returned by these tools) and assert none of
    them is 530.0 — the blended DKK+EUR total this fix eliminates."""
    if isinstance(payload, dict):
        for v in payload.values():
            assert_no_blend(v)
    elif isinstance(payload, list):
        for v in payload:
            assert_no_blend(v)
    elif isinstance(payload, (int, float)) and not isinstance(payload, bool):
        assert payload != 530.0, f"found blended total 530.0 in payload: {payload!r}"


def test_get_spending_total_is_per_currency_dict(storage, monkeypatch):
    import jyske_mcp.mcp.server as server

    day = _this_month_day(storage)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=300.0, currency="DKK", day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Groceries",
               amount=200.0, currency="DKK", day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=30.0, currency="EUR", day=day)

    import json
    payload = json.loads(server.get_spending())

    assert payload["total"] == {"DKK": 500.0, "EUR": 30.0}
    assert 530.0 not in payload["total"].values()
    assert payload["count"] == 3
    assert_no_blend(payload)


def test_compare_spending_totals_keyed_by_currency(storage):
    import jyske_mcp.mcp.server as server

    day = _this_month_day(storage)
    baseline_day = _prev_month_day(storage)

    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=300.0, currency="DKK", day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Groceries",
               amount=200.0, currency="DKK", day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=30.0, currency="EUR", day=day)

    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=250.0, currency="DKK", day=baseline_day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=20.0, currency="EUR", day=baseline_day)

    import json
    month_start, _ = storage.current_month_window()
    month = month_start[:7]
    payload = json.loads(server.compare_spending(month=month))

    totals = payload["totals"]
    assert set(totals) == {"DKK", "EUR"}
    assert totals["DKK"]["current"] == 500.0
    assert totals["EUR"]["current"] == 30.0
    for block in totals.values():
        assert block["current"] != 530.0
    assert_no_blend(payload)


def test_get_budget_status_dkk_only_with_other_currency_amounts(storage):
    day = _this_month_day(storage)

    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=300.0, currency="DKK", day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Groceries",
               amount=200.0, currency="DKK", day=day)
    _insert_tx(storage, category_top="Food & Dining", category_mid="Restaurants",
               amount=30.0, currency="EUR", day=day)

    # A DKK-only category, to confirm other_currency_amounts is omitted
    # entirely (not present as an empty dict) when there's nothing non-DKK.
    _insert_tx(storage, category_top="Transport", category_mid="Fuel",
               amount=100.0, currency="DKK", day=day)

    storage.set_budget(category_top="Food & Dining", limit_amount=1000.0,
                        period="monthly", agent_id="finance")
    storage.set_budget(category_top="Transport", limit_amount=500.0,
                        period="monthly", agent_id="finance")

    statuses = storage.get_budget_status(agent_id="finance")
    assert len(statuses) == 2

    food = next(s for s in statuses if s["category"] == "Food & Dining")
    assert food["spent"] == 500.0
    assert food["other_currency_amounts"] == {"EUR": 30.0}
    assert food["percent"] == 50.0
    assert food["status"] == "on_track"

    transport = next(s for s in statuses if s["category"] == "Transport")
    assert transport["spent"] == 100.0
    assert "other_currency_amounts" not in transport

    assert_no_blend(statuses)
