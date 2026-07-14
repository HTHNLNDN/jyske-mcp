"""
Characterization tests (golden master, FastAPI TestClient) for the
money-math HTTP endpoints in jyske_mcp/web/app.py: /budgets/status,
/budgets/breakdown, /budgets/transactions, /goals. Pins the exact JSON
shapes these endpoints return today, including the DKK-primary /
other_currency_amounts de-blending (see PRIMARY_CURRENCY in
slices/finance/storage.py) and
the "uncategorized always sorts last" behavior of /budgets/breakdown — a
future VSA relocation of this code must keep producing byte-identical
output, since the Vue frontend consumes these shapes directly.

Every route below requires an authenticated session (jyske_mcp/web/app.py's
AuthMiddleware default-denies), so each test logs in via /auth/login first
using os.environ["APP_PIN"] (set by tests/conftest.py) and resets the
module-level failed-attempt/lockout counters beforehand so state left over
by other tests can never cause a spurious 429 here.

No live Enable Banking or LLM calls anywhere in this file — every route
under test reads only from the temp SQLite DB seeded below (via
full_schema_storage, tests/conftest.py).
"""
import os
import sqlite3
import time

import jyske_mcp.kernel.storage as storage_module
import jyske_mcp.web.app as app_module
from fastapi.testclient import TestClient

APP_PIN = os.environ["APP_PIN"]


def _authed_client() -> TestClient:
    # Clean slate regardless of what any other test in this process may
    # have left in these module globals (see test_auth_login_lockout.py).
    app_module._failed_attempts = 0
    app_module._lockout_until = 0.0
    client = TestClient(app_module.app)
    resp = client.post("/auth/login", json={"pin": APP_PIN})
    assert resp.status_code == 200, resp.text
    return client


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


def _this_month_day(storage, day: int = 5) -> str:
    month_start, _today = storage.current_month_window()
    return f"{month_start[:7]}-{day:02d}"


def test_budgets_status_pins_dkk_primary_and_other_currency_shape(full_schema_storage):
    day = _this_month_day(full_schema_storage)

    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=300.0, currency="DKK", day=day)
    _insert_tx(category_top="Food & Dining", category_mid="Groceries", amount=200.0, currency="DKK", day=day)
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=30.0, currency="EUR", day=day)
    _insert_tx(category_top="Transport", category_mid="Fuel", amount=100.0, currency="DKK", day=day)

    full_schema_storage.set_budget(category_top="Food & Dining", limit_amount=1000.0, period="monthly")
    full_schema_storage.set_budget(category_top="Transport", limit_amount=500.0, period="monthly")

    client = _authed_client()
    resp = client.get("/budgets/status")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"budgets"}
    assert len(body["budgets"]) == 2

    by_category = {b["category"]: b for b in body["budgets"]}
    assert set(by_category) == {"Food & Dining", "Transport"}

    # `id` (the underlying budgets.id row id, needed by the frontend to call
    # DELETE /budgets/{id}) is asserted separately as "some int" rather than
    # hardcoded — everything else about the shape is still pinned exactly.
    food = by_category["Food & Dining"]
    food_id = food.pop("id")
    assert isinstance(food_id, int)
    assert food == {
        "category": "Food & Dining",
        "category_mid": None,
        "spent": 500.0,
        "limit": 1000.0,
        "period": "monthly",
        "percent": 50.0,
        "status": "on_track",
        "other_currency_amounts": {"EUR": 30.0},
    }

    transport = by_category["Transport"]
    transport_id = transport.pop("id")
    assert isinstance(transport_id, int)
    assert transport_id != food_id
    assert transport == {
        "category": "Transport",
        "category_mid": None,
        "spent": 100.0,
        "limit": 500.0,
        "period": "monthly",
        "percent": 20.0,
        "status": "on_track",
    }
    assert "other_currency_amounts" not in transport


def test_budgets_breakdown_pins_shape_and_uncategorized_always_sorts_last(full_schema_storage):
    day = _this_month_day(full_schema_storage)

    # Uncategorized is seeded as the LARGEST amount specifically to prove it
    # still sorts after every real mid-category regardless of size (current
    # behavior — see the explicit `if uncategorized_entry: breakdown.append(...)`
    # placed AFTER the by-spend sort in budgets_breakdown()).
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=180.0, currency="DKK", day=day)
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=120.0, currency="DKK", day=day)
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=30.0, currency="EUR", day=day)
    _insert_tx(category_top="Food & Dining", category_mid="Groceries", amount=200.0, currency="DKK", day=day)
    _insert_tx(category_top="Food & Dining", category_mid=None, amount=500.0, currency="DKK", day=day)

    client = _authed_client()
    resp = client.get("/budgets/breakdown", params={"category": "Food & Dining"})

    assert resp.status_code == 200
    body = resp.json()

    month_start, today = full_schema_storage.current_month_window()
    assert body["category"] == "Food & Dining"
    assert body["period_from"] == month_start
    assert body["period_to"] == today
    assert body["total"] == 1000.0  # 300 (Restaurants) + 200 (Groceries) + 500 (Uncategorized)
    assert body["other_currency_amounts"] == {"EUR": 30.0}

    assert body["breakdown"] == [
        {
            "category_mid": "Restaurants",
            "label": "Restaurants",
            "spent": 300.0,
            "count": 3,
            "uncategorized": False,
            "other_currency_amounts": {"EUR": 30.0},
        },
        {
            "category_mid": "Groceries",
            "label": "Groceries",
            "spent": 200.0,
            "count": 1,
            "uncategorized": False,
        },
        {
            "category_mid": None,
            "label": "Uncategorized",
            "spent": 500.0,
            "count": 1,
            "uncategorized": True,
        },
    ]


def test_budgets_transactions_line_items_mirror_breakdown_and_ignore_currency_in_mid_filter(full_schema_storage):
    day1 = _this_month_day(full_schema_storage, day=3)
    day2 = _this_month_day(full_schema_storage, day=8)

    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=180.0, currency="DKK", day=day2)
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants", amount=30.0, currency="EUR", day=day1)
    _insert_tx(category_top="Food & Dining", category_mid=None, amount=500.0, currency="DKK", day=day1)

    client = _authed_client()

    resp = client.get(
        "/budgets/transactions",
        params={"category": "Food & Dining", "mid": "Restaurants"},
    )
    assert resp.status_code == 200
    body = resp.json()
    month_start, today = full_schema_storage.current_month_window()
    assert body["category"] == "Food & Dining"
    assert body["category_mid"] == "Restaurants"
    assert body["period_from"] == month_start
    assert body["period_to"] == today

    # mid filtering does NOT filter by currency — both the DKK and EUR
    # Restaurants transactions come back, newest date first.
    items = body["items"]
    assert [(i["amount"], i["currency"]) for i in items] == [(180.0, "DKK"), (30.0, "EUR")]
    assert sum(i["amount"] for i in items if i["currency"] == "DKK") == 180.0

    resp_uncat = client.get(
        "/budgets/transactions",
        params={"category": "Food & Dining", "uncategorized": "true"},
    )
    assert resp_uncat.status_code == 200
    uncat_items = resp_uncat.json()["items"]
    assert len(uncat_items) == 1
    assert uncat_items[0]["amount"] == 500.0
    assert resp_uncat.json()["category_mid"] is None


def test_goals_endpoint_joins_goal_pace_by_id(full_schema_storage):
    from datetime import datetime, timedelta, timezone

    def _midnight_utc(days_from_today: int):
        today = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
        return datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    conn = sqlite3.connect(str(storage_module._CACHE_DB))

    # Alpha: 30 days into a 90-day goal, exactly on the expected-by-now pace
    # -> deterministically "on_track" (mirrors tests/test_goal_pace.py's
    # proven-stable on_track fixture).
    created_a = _midnight_utc(-30)
    deadline_a = _midnight_utc(60)
    conn.execute(
        "INSERT INTO goals (agent_id, name, target_amount, current_amount, purpose, deadline, "
        "active, created_at, updated_at) VALUES ('finance', 'Alpha', 900.0, 300.0, 'p', ?, 1, ?, ?)",
        (deadline_a.strftime("%Y-%m-%d"), created_a.timestamp(), created_a.timestamp()),
    )
    id_a = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Beta: current already >= target -> deterministically "complete"
    # regardless of what "now" is.
    created_b = _midnight_utc(-100)
    deadline_b = _midnight_utc(-10)
    conn.execute(
        "INSERT INTO goals (agent_id, name, target_amount, current_amount, purpose, deadline, "
        "active, created_at, updated_at) VALUES ('finance', 'Beta', 500.0, 500.0, 'p', ?, 1, ?, ?)",
        (deadline_b.strftime("%Y-%m-%d"), created_b.timestamp(), created_b.timestamp()),
    )
    id_b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    client = _authed_client()
    resp = client.get("/goals")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"goals"}
    assert len(body["goals"]) == 2

    by_name = {g["name"]: g for g in body["goals"]}
    assert set(by_name) == {"Alpha", "Beta"}

    alpha = by_name["Alpha"]
    assert alpha["id"] == id_a
    assert alpha["target_amount"] == 900.0
    assert alpha["current_amount"] == 300.0
    assert alpha["pace"]["goal_id"] == id_a
    assert alpha["pace"]["name"] == "Alpha"
    assert alpha["pace"]["status"] == "on_track"
    assert alpha["pace"]["pct_complete"] == round(300.0 / 900.0 * 100, 1)

    beta = by_name["Beta"]
    assert beta["id"] == id_b
    assert beta["pace"]["goal_id"] == id_b
    assert beta["pace"]["name"] == "Beta"
    assert beta["pace"]["status"] == "complete"
    assert beta["pace"]["pct_complete"] == 100.0
