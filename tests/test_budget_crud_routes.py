"""
Covers the new dashboard budget-creation/delete surface added alongside the
mid-level categorization-taxonomy fix:
  - POST /budgets (create/edit — set_budget() already replaces-on-conflict
    for the same category_top+category_mid+period, so re-POSTing IS the
    edit path, no separate endpoint).
  - DELETE /budgets/{budget_id} (soft-delete via Storage.deactivate_budget).
  - Storage.deactivate_budget itself, including its agent_id scoping.
  - /budgets/recategorize's refactor onto validate_category_pair() —
    behavior-preservation check that 400-on-invalid-top/mid didn't change.

Every route below requires an authenticated session (jyske_mcp/web/app.py's
AuthMiddleware default-denies) — same login pattern as
tests/test_budget_and_goal_endpoints.py. No live Enable Banking or LLM
calls anywhere in this file.
"""
import os
import sqlite3
import time

import jyske_mcp.kernel.categorizer as categorizer_module
import jyske_mcp.kernel.storage as storage_module
import jyske_mcp.web.app as app_module
from fastapi.testclient import TestClient

APP_PIN = os.environ["APP_PIN"]


def _authed_client() -> TestClient:
    app_module._failed_attempts = 0
    app_module._lockout_until = 0.0
    client = TestClient(app_module.app)
    resp = client.post("/auth/login", json={"pin": APP_PIN})
    assert resp.status_code == 200, resp.text
    return client


def _insert_tx(*, category_top, category_mid, amount, currency="DKK", day, direction="DBIT", account_uid="acc1"):
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO transactions "
        "(account_uid, transaction_id, date, amount, currency, description, "
        " category_top, category_mid, created_at, direction) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ",
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
    tx_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return tx_id


def _this_month_day(storage, day: int = 5) -> str:
    month_start, _today = storage.current_month_window()
    return f"{month_start[:7]}-{day:02d}"


# ── POST /budgets ────────────────────────────────────────────────────────────

def test_create_budget_top_level_only(full_schema_storage):
    client = _authed_client()
    resp = client.post("/budgets", json={"category_top": "Transport", "limit_amount": 500.0})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["category_top"] == "Transport"
    assert body["category_mid"] is None
    assert body["limit_amount"] == 500.0
    assert body["period"] == "monthly"

    budgets = full_schema_storage.get_budgets()
    assert len(budgets) == 1
    assert budgets[0].category_top == "Transport"
    assert budgets[0].category_mid is None


def test_create_budget_with_valid_mid(full_schema_storage, monkeypatch):
    # v1's shipped categories.json has no real mid-level content under any
    # top (see .agent/epics/custom-categorization.md) — the top/mid
    # mechanism itself is kept for a future deliverable, so this test
    # injects a mid under a real top to prove the mechanism still accepts
    # a valid (top, mid) pair, rather than asserting on content that
    # doesn't exist yet. category_tree() is called first to force a real
    # load before the override, so this doesn't depend on some other test
    # module having already triggered it.
    categorizer_module.category_tree()
    monkeypatch.setitem(categorizer_module._category_tree, "Eating Out", ["Restaurants"])

    client = _authed_client()
    resp = client.post(
        "/budgets",
        json={"category_top": "Eating Out", "category_mid": "Restaurants", "limit_amount": 800.0},
    )

    assert resp.status_code == 200
    budgets = full_schema_storage.get_budgets()
    assert len(budgets) == 1
    assert budgets[0].category_top == "Eating Out"
    assert budgets[0].category_mid == "Restaurants"


def test_create_budget_reposting_same_combo_edits_in_place(full_schema_storage):
    client = _authed_client()
    resp1 = client.post("/budgets", json={"category_top": "Transport", "limit_amount": 500.0})
    assert resp1.status_code == 200

    resp2 = client.post("/budgets", json={"category_top": "Transport", "limit_amount": 750.0})
    assert resp2.status_code == 200

    # Only one active row remains, at the NEW limit — the old one was
    # deactivated by set_budget's replace-on-conflict, not stacked.
    budgets = full_schema_storage.get_budgets()
    assert len(budgets) == 1
    assert budgets[0].limit_amount == 750.0


def test_create_budget_unknown_top_returns_400(full_schema_storage):
    client = _authed_client()
    resp = client.post("/budgets", json={"category_top": "Not A Real Category", "limit_amount": 500.0})

    assert resp.status_code == 400
    assert "Not A Real Category" in resp.json()["detail"]
    assert full_schema_storage.get_budgets() == []


def test_create_budget_unknown_mid_for_top_returns_400(full_schema_storage):
    # "Eating Out" is a real top, but v1's categories.json has no mid-level
    # content under any top (empty list) — any non-blank mid is invalid.
    client = _authed_client()
    resp = client.post(
        "/budgets",
        json={"category_top": "Eating Out", "category_mid": "Restaurants & Cafes", "limit_amount": 800.0},
    )

    assert resp.status_code == 400
    assert "Restaurants & Cafes" in resp.json()["detail"]
    assert full_schema_storage.get_budgets() == []


def test_create_budget_non_positive_limit_returns_400(full_schema_storage):
    client = _authed_client()
    resp = client.post("/budgets", json={"category_top": "Transport", "limit_amount": 0})

    assert resp.status_code == 400
    assert full_schema_storage.get_budgets() == []


def test_create_budget_invalid_period_returns_400(full_schema_storage):
    client = _authed_client()
    resp = client.post(
        "/budgets", json={"category_top": "Transport", "limit_amount": 500.0, "period": "yearly"}
    )

    assert resp.status_code == 400
    assert full_schema_storage.get_budgets() == []


# ── DELETE /budgets/{budget_id} ──────────────────────────────────────────────

def test_delete_budget_soft_deactivates(full_schema_storage):
    full_schema_storage.set_budget(category_top="Transport", limit_amount=500.0, period="monthly")
    budget_id = full_schema_storage.get_budgets()[0].id

    client = _authed_client()
    resp = client.delete(f"/budgets/{budget_id}")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert full_schema_storage.get_budgets() == []

    # Soft-delete, not a hard DELETE — the row still exists with active=0.
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    row = conn.execute("SELECT active FROM budgets WHERE id = ?", (budget_id,)).fetchone()
    conn.close()
    assert row == (0,)


def test_delete_unknown_budget_returns_404(full_schema_storage):
    client = _authed_client()
    resp = client.delete("/budgets/99999")

    assert resp.status_code == 404


def test_delete_already_deleted_budget_returns_404(full_schema_storage):
    full_schema_storage.set_budget(category_top="Transport", limit_amount=500.0, period="monthly")
    budget_id = full_schema_storage.get_budgets()[0].id

    client = _authed_client()
    first = client.delete(f"/budgets/{budget_id}")
    assert first.status_code == 200

    second = client.delete(f"/budgets/{budget_id}")
    assert second.status_code == 404


def test_deactivate_budget_is_scoped_by_agent_id(full_schema_storage):
    # Defense-in-depth check: a budget belonging to a different agent_id
    # must not be deactivated by a call scoped to "finance".
    full_schema_storage.set_budget(category_top="Transport", limit_amount=500.0, period="monthly", agent_id="fashion")
    budget_id = full_schema_storage.get_budgets(agent_id="fashion")[0].id

    changed = full_schema_storage.deactivate_budget(budget_id, agent_id="finance")

    assert changed is False
    assert len(full_schema_storage.get_budgets(agent_id="fashion")) == 1


def test_deactivate_budget_returns_true_once_then_false(full_schema_storage):
    full_schema_storage.set_budget(category_top="Transport", limit_amount=500.0, period="monthly")
    budget_id = full_schema_storage.get_budgets()[0].id

    assert full_schema_storage.deactivate_budget(budget_id) is True
    assert full_schema_storage.deactivate_budget(budget_id) is False


def test_delete_budget_id_present_in_get_budget_status(full_schema_storage):
    client = _authed_client()
    client.post("/budgets", json={"category_top": "Transport", "limit_amount": 500.0})

    status_resp = client.get("/budgets/status")
    assert status_resp.status_code == 200
    row = status_resp.json()["budgets"][0]
    budget_id = row["id"]

    del_resp = client.delete(f"/budgets/{budget_id}")
    assert del_resp.status_code == 200

    status_resp2 = client.get("/budgets/status")
    assert status_resp2.json()["budgets"] == []


# ── /budgets/recategorize behavior preservation ──────────────────────────────

def test_recategorize_unknown_top_still_400(full_schema_storage):
    day = _this_month_day(full_schema_storage)
    tx_id = _insert_tx(category_top="Eating Out", category_mid="Restaurants", amount=100.0, day=day)

    client = _authed_client()
    resp = client.post(
        "/budgets/recategorize",
        json={"transaction_id": tx_id, "category_top": "Not A Real Category", "category_mid": "Whatever"},
    )
    assert resp.status_code == 400
    assert "Not A Real Category" in resp.json()["detail"]


def test_recategorize_unknown_mid_still_400(full_schema_storage):
    # "Eating Out" is a real top, but v1's categories.json has no mid-level
    # content under any top (empty list) — any non-blank mid is invalid.
    day = _this_month_day(full_schema_storage)
    tx_id = _insert_tx(category_top="Eating Out", category_mid="Restaurants", amount=100.0, day=day)

    client = _authed_client()
    resp = client.post(
        "/budgets/recategorize",
        json={"transaction_id": tx_id, "category_top": "Eating Out", "category_mid": "Restaurants & Cafes"},
    )
    assert resp.status_code == 400
    assert "Restaurants & Cafes" in resp.json()["detail"]


def test_recategorize_empty_mid_now_succeeds(full_schema_storage):
    # category_mid used to be a required field for recategorize — an empty
    # string was rejected with a 400 even though validate_category_pair()
    # itself treats a falsy mid as valid everywhere else (e.g. POST
    # /budgets). That special-case requirement was relaxed once the
    # custom-categorization taxonomy shipped every top-level category with
    # zero mid-level entries in v1 (discovered live: it made this endpoint
    # unusable for any new category, since there was never a valid non-blank
    # mid to pick). category_mid=None/"" is now a legitimate "recategorize to
    # this top-level category, no sub-category" request, same contract as
    # every other categorization write path.
    day = _this_month_day(full_schema_storage)
    tx_id = _insert_tx(category_top="Eating Out", category_mid="Restaurants", amount=100.0, day=day)

    client = _authed_client()
    resp = client.post(
        "/budgets/recategorize",
        json={"transaction_id": tx_id, "category_top": "Bills", "category_mid": ""},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["new_category_top"] == "Bills"
    assert body["new_category_mid"] is None


def test_recategorize_valid_top_and_mid_succeeds(full_schema_storage, monkeypatch):
    # v1's shipped categories.json has no real mid-level content under any
    # top — inject one under a real top to prove the mechanism (kept for a
    # future deliverable) still accepts a valid (top, mid) pair. Force a
    # real load first so this doesn't depend on test execution order.
    categorizer_module.category_tree()
    monkeypatch.setitem(categorizer_module._category_tree, "Eating Out", ["Restaurants", "Takeaway"])

    day = _this_month_day(full_schema_storage)
    tx_id = _insert_tx(category_top="Eating Out", category_mid="Takeaway", amount=100.0, day=day)

    client = _authed_client()
    resp = client.post(
        "/budgets/recategorize",
        json={"transaction_id": tx_id, "category_top": "Eating Out", "category_mid": "Restaurants"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["new_category_top"] == "Eating Out"
    assert body["new_category_mid"] == "Restaurants"
