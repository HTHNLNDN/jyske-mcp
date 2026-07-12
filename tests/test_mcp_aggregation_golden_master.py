"""
Characterization tests (golden master) for the three chat-tool aggregation
functions in jyske_mcp/slices/finance/tools.py: get_spending,
compare_spending, goal_pace. Called directly as plain functions (there is
no MCP transport — see epic deliverable #8, FastMCP was retired) per this
suite's existing convention (see tests/test_goal_pace.py,
tests/test_mixed_currency_no_blend.py).

These pin the COMPLETE json.loads() payload — every key, exactly — rather
than spot-checking individual fields the way the existing correctness
suite (tests/test_sum_spending.py, tests/test_goal_pace.py,
tests/test_mixed_currency_no_blend.py, tests/test_compare_spending_proration.py)
already does. That existing suite is not duplicated here; this file adds
the "no key was added/removed/renamed" shape guarantee those files don't
make.

get_spending/compare_spending are called with EXPLICIT date_from/date_to
(resp. month/baseline_month) windows fixed safely in the past, rather than
relying on "this calendar month" defaults — that sidesteps two real
flakiness sources: (a) a seeded day-of-month later than "today" would
silently fall outside get_spending's default date_to and be dropped from
the aggregate, and (b) compare_spending's in_progress/baseline_prorated
proration path (already covered by tests/test_compare_spending_proration.py)
only activates for the CURRENT calendar month — using a fixed past month
keeps this file's expected payload shape constant regardless of when it
runs. goal_pace has no such override (it always reads datetime.now()
internally), so its test instead reuses the proven midnight-anchored
relative-day technique from tests/test_goal_pace.py.
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import jyske_mcp.kernel.storage as storage_module
import jyske_mcp.slices.finance.tools as server


def _insert_tx(*, category_top, category_mid, amount, currency, day, direction="DBIT", account_uid="acc1"):
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO transactions "
        "(account_uid, transaction_id, date, amount, currency, description, "
        " category_top, category_mid, created_at, direction) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_uid,
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


def _sorted_breakdown(rows: list[dict]) -> list[dict]:
    # sum_spending()'s SQL has no ORDER BY (see jyske_mcp/slices/finance/storage.py) — its
    # GROUP BY row order is an incidental SQLite implementation detail, not
    # a contract, so breakdown rows are compared sorted rather than
    # position-by-position.
    return sorted(rows, key=lambda r: (r["key"] or "", r["currency"]))


def test_get_spending_full_json_shape_default_group_by_category(full_schema_storage):
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=120.50, currency="DKK", day="2020-03-03")
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=80.25, currency="DKK", day="2020-03-10")
    _insert_tx(category_top="Food & Dining", category_mid="Groceries", amount=340.00, currency="DKK", day="2020-03-05")
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=45.00, currency="EUR", day="2020-03-07")
    _insert_tx(category_top="Transport", category_mid="Fuel", amount=275.30, currency="DKK", day="2020-03-12")
    # Outside the window and a credit — neither should be counted.
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=999.0, currency="DKK", day="2020-04-01")
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=999.0, currency="DKK", day="2020-03-15", direction="CRDT")

    payload = json.loads(server.get_spending(date_from="2020-03-01", date_to="2020-03-31"))

    assert set(payload.keys()) == {"date_from", "date_to", "group_by", "total", "count", "breakdown"}
    assert payload["date_from"] == "2020-03-01"
    assert payload["date_to"] == "2020-03-31"
    assert payload["group_by"] == "category"
    assert payload["total"] == {"DKK": 816.05, "EUR": 45.0}
    assert payload["count"] == 5

    assert _sorted_breakdown(payload["breakdown"]) == [
        {"key": "Food & Dining", "currency": "DKK", "amount": 540.75, "count": 3},
        {"key": "Food & Dining", "currency": "EUR", "amount": 45.0, "count": 1},
        {"key": "Transport", "currency": "DKK", "amount": 275.3, "count": 1},
    ]


def test_get_spending_full_json_shape_group_by_mid_with_category_filter(full_schema_storage):
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=100.0, currency="DKK", day="2020-03-03")
    _insert_tx(category_top="Food & Dining", category_mid="Groceries", amount=250.0, currency="DKK", day="2020-03-05")
    _insert_tx(category_top="Transport", category_mid="Fuel", amount=999.0, currency="DKK", day="2020-03-06")  # different top -> excluded

    payload = json.loads(server.get_spending(
        date_from="2020-03-01", date_to="2020-03-31",
        category="Food & Dining", group_by="mid",
    ))

    assert payload["group_by"] == "mid"
    assert payload["total"] == {"DKK": 350.0}
    assert payload["count"] == 2
    assert _sorted_breakdown(payload["breakdown"]) == [
        {"key": "Groceries", "currency": "DKK", "amount": 250.0, "count": 1},
        {"key": "Restaurants", "currency": "DKK", "amount": 100.0, "count": 1},
    ]


def test_compare_spending_full_json_shape_two_past_months(full_schema_storage):
    # Both months are safely in the past, so in_progress is deterministically
    # False (no baseline_prorated/low_confidence keys) regardless of today's
    # real date — see tests/test_compare_spending_proration.py for that
    # branch's own dedicated coverage.
    _insert_tx(category_top="Food & Dining", amount=400.0, currency="DKK", day="2020-03-05", category_mid=None)
    _insert_tx(category_top="Transport", amount=150.0, currency="DKK", day="2020-03-08", category_mid=None)
    _insert_tx(category_top="Food & Dining", amount=300.0, currency="DKK", day="2020-02-05", category_mid=None)
    _insert_tx(category_top="Transport", amount=200.0, currency="DKK", day="2020-02-08", category_mid=None)

    payload = json.loads(server.compare_spending(month="2020-03", baseline_month="2020-02"))

    assert set(payload.keys()) == {"month", "baseline_month", "group_by", "totals", "breakdown"}
    assert payload["month"] == "2020-03"
    assert payload["baseline_month"] == "2020-02"
    assert payload["group_by"] == "category"
    assert payload["totals"] == {
        "DKK": {"current": 550.0, "baseline": 500.0, "delta": 50.0, "pct_change": 10.0},
    }
    # abs(delta) for Food & Dining (100) != Transport (50) — a real,
    # non-tied Python .sort(key=lambda r: abs(r["delta"]), reverse=True), so
    # this order is deterministic and safe to assert positionally (unlike
    # get_spending/sum_spending's bare-SQL GROUP BY order above).
    assert payload["breakdown"] == [
        {
            "category": "Food & Dining", "currency": "DKK",
            "current": 400.0, "baseline": 300.0, "delta": 100.0, "pct_change": 33.3,
        },
        {
            "category": "Transport", "currency": "DKK",
            "current": 150.0, "baseline": 200.0, "delta": -50.0, "pct_change": -25.0,
        },
    ]


def _midnight_utc(days_from_today: int) -> datetime:
    today = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    return datetime(today.year, today.month, today.day, tzinfo=timezone.utc)


def test_goal_pace_full_json_shape_on_track_goal(full_schema_storage):
    # Same fixture as tests/test_goal_pace.py's proven-stable
    # test_on_track_status: created 30 days ago, deadline 60 days out,
    # target=900/current=300 lands exactly on the expected-by-now pace.
    created_at = _midnight_utc(-30)
    deadline = _midnight_utc(60)
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO goals (agent_id, name, target_amount, current_amount, purpose, deadline, "
        "active, created_at, updated_at) VALUES ('finance', 'Emergency fund', 900.0, 300.0, "
        "'safety net', ?, 1, ?, ?)",
        (deadline.strftime("%Y-%m-%d"), created_at.timestamp(), created_at.timestamp()),
    )
    goal_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    results = json.loads(server.goal_pace())

    assert len(results) == 1
    r = results[0]
    assert set(r.keys()) == {
        "goal_id", "name", "status", "pct_complete", "days_remaining",
        "required_daily", "required_monthly", "expected_now", "projected_completion_date",
    }

    # Fully deterministic fields (no dependency on time-of-day, only on
    # calendar days via midnight-anchored created_at/deadline).
    assert r["goal_id"] == goal_id
    assert r["name"] == "Emergency fund"
    assert r["status"] == "on_track"
    assert r["pct_complete"] == 33.3
    assert r["expected_now"] == 300.0
    # days_elapsed=30, daily_rate=300/30=10, days_to_target=900/10=90 —
    # created_at + 90 days is itself midnight-exact, so this is exact too.
    assert r["projected_completion_date"] == (created_at + timedelta(days=90)).strftime("%Y-%m-%d")

    # days_remaining = (deadline_date - now).days is NOT floor-clean (now
    # isn't midnight-anchored) — tests/test_goal_pace.py deliberately never
    # pins this to an exact number either. Pin its ballpark plus
    # required_daily/monthly's self-consistency with whatever it actually is.
    assert r["days_remaining"] in (59, 60)
    amount_remaining = round(900.0 - 300.0, 2)
    assert r["required_daily"] == round(amount_remaining / r["days_remaining"], 2)
    assert r["required_monthly"] == round(r["required_daily"] * 30.4, 2)
