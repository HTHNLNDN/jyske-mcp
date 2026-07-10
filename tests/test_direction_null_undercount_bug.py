"""
CHARACTERIZATION TEST — pins a KNOWN, NOT-YET-FIXED bug. Do not "fix" the
assertions below to what the math *should* be; that would defeat the point
of this file.

Storage.sum_spending() (jyske_mcp/storage.py) — the aggregation underlying
the get_spending / compare_spending MCP tools and get_budget_status —
filters with `WHERE direction != 'CRDT'`. In SQLite, `NULL != 'CRDT'`
evaluates to NULL (neither true nor false), so any transaction row whose
`direction` column is NULL is SILENTLY EXCLUDED from every spending
aggregate, even when it's a genuine debit that should count. `direction`
is nullable (see jyske_mcp/storage.py: `tx.get("credit_debit_indicator")`
is stored as-is, with no NOT NULL constraint — migrations/versions/
784418892304_add_math_aggregation_tools.py added the column as nullable),
so this isn't a hypothetical: any transaction whose raw Enable Banking
payload lacks credit_debit_indicator lands with direction=NULL and quietly
stops counting toward spending/budget totals.

Tracked separately in .agent/epics/vsa-restructure.md's "Related backlog"
as "Fix direction != 'CRDT' NULL under-count in spending aggregation
(storage.sum_spending and siblings) + regression test" — this file is the
BASELINE that fix will need to update (the assertions here should flip from
"excluded" to "included" once it lands), not a regression this suite should
block.
"""
import sqlite3
import time

import jyske_mcp.storage as storage_module
import jyske_mcp.mcp.server as server


def _insert_tx(*, category_top, category_mid, amount, currency, day, direction, account_uid="acc1"):
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    conn.execute(
        "INSERT INTO transactions "
        "(account_uid, transaction_id, date, amount, currency, description, "
        " category_top, category_mid, created_at, direction) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_uid,
            f"tx-{category_mid}-{currency}-{day}-{amount}-{direction}-{time.time()}",
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


def test_sum_spending_silently_excludes_null_direction_debit(full_schema_storage):
    day = _this_month_day(full_schema_storage)

    # A normal, correctly-tagged debit.
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants",
               amount=100.0, currency="DKK", day=day, direction="DBIT")
    # A genuine debit whose direction never got set (e.g. Enable Banking
    # omitted credit_debit_indicator on this row) — this SHOULD count
    # toward spending too, but the `direction != 'CRDT'` filter drops it
    # because NULL != 'CRDT' is NULL, not true.
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants",
               amount=900.0, currency="DKK", day=day, direction=None)

    rows = full_schema_storage.sum_spending(
        full_schema_storage.current_month_window()[0],
        full_schema_storage.current_month_window()[1],
        group_by="category",
    )

    # BUG (pinned, not fixed): only the 100.0 DBIT row counts. The 900.0
    # NULL-direction row is real spend that the query silently drops —
    # correct behavior would be amount == 1000.0.
    assert len(rows) == 1
    assert rows[0]["amount"] == 100.0


def test_get_spending_tool_undercounts_with_null_direction_row(full_schema_storage):
    day = _this_month_day(full_schema_storage)

    _insert_tx(category_top="Food & Dining", category_mid="Restaurants",
               amount=100.0, currency="DKK", day=day, direction="DBIT")
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants",
               amount=900.0, currency="DKK", day=day, direction=None)

    import json
    payload = json.loads(server.get_spending())

    # BUG (pinned): total/count reflect only the DBIT row.
    assert payload["total"] == {"DKK": 100.0}
    assert payload["count"] == 1


def test_get_budget_status_understates_spend_and_hides_overspend_with_null_direction(full_schema_storage):
    day = _this_month_day(full_schema_storage)

    # A single NULL-direction debit that's already well over a 500 DKK
    # budget on its own.
    _insert_tx(category_top="Food & Dining", category_mid="Restaurants",
               amount=1000.0, currency="DKK", day=day, direction=None)

    full_schema_storage.set_budget(category_top="Food & Dining", limit_amount=500.0, period="monthly")

    statuses = full_schema_storage.get_budget_status()

    assert len(statuses) == 1
    status = statuses[0]
    # BUG (pinned): the NULL-direction 1000 DKK charge is invisible to the
    # aggregate entirely, so a budget that's actually 200% over reports as
    # untouched — spent=0.0, "on_track", not "over". A real user would not
    # be warned about this overspend.
    assert status["spent"] == 0.0
    assert status["percent"] == 0.0
    assert status["status"] == "on_track"
