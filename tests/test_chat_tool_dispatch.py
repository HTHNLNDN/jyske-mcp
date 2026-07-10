"""
Characterization tests for jyske_mcp/web/app.py's _run_tool — the chat
tool-call dispatcher that maps an LLM-issued tool name + JSON args to one
of the 23 MCP tool functions. No litellm/LLM call happens anywhere in this
file (_run_tool itself never calls an LLM — the 23 functions it dispatches
to only ever touch local SQLite via Storage, per the "MCP tools never call
Enable Banking" rule), so there's nothing to mock there; the zero-cost
constraint is satisfied simply by never invoking chat()/chat_completion.

Two layers of coverage, deliberately kept separate:

  1. Dispatch-table pinning (mock-based, no DB): each of the 23 tool names
     is proven to route to the correspondingly-named function with the
     input dict forwarded exactly as-is (or, for the 9 zero-arg tools,
     proven that the function is called with NO arguments and any input
     keys are silently ignored — current behavior, worth pinning since a
     VSA-era registry refactor (epic deliverable #8) could easily change
     this). This is what most precisely answers "dispatches to the right
     function" and is immune to any real function's own business-logic
     changes.

  2. A real end-to-end smoke test (full_schema_storage, no mocks) proving
     all 23 really execute without the _run_tool try/except's "Tool error
     (...)" fallback firing, against a small seeded dataset covering every
     tool's data dependency.
"""
from unittest.mock import MagicMock

import pytest

import jyske_mcp.web.app as app_module

# Tools whose _run_tool lambda calls fn() with no arguments, ignoring
# whatever `inputs` dict was passed (see jyske_mcp/web/app.py's dispatch table).
ZERO_ARG_TOOLS = [
    "get_memory",
    "list_accounts",
    "get_sync_status",
    "get_budget_status",
    "get_goals",
    "get_onboarding_status",
    "complete_onboarding",
    "get_overspend_patterns",
    "get_current_tip",
]

# Tools whose _run_tool lambda calls fn(**inputs) — the exact kwargs each
# needs, matching jyske_mcp/mcp/server.py's real signatures.
KWARG_TOOLS = {
    "get_balances": {"account_uid": "acc-1"},
    "get_transactions": {"account_uid": "acc-1", "date_from": "2020-01-01", "date_to": "2020-01-31"},
    "categorize_transaction": {"raw_name": "Some Shop", "mcc": "5411"},
    "set_budget": {"category": "Food & Dining", "limit_amount": 500.0, "period": "monthly"},
    "update_memory": {"session_summary": "did stuff"},
    "set_goal": {"name": "Trip", "target_amount": 1000.0, "purpose": "vacation", "deadline": "2027-01-01"},
    "update_goal_progress": {"goal_id": 1, "current_amount": 250.0},
    "set_onboarding_stage": {"stage": "fixed_costs", "income": 20000.0},
    "get_spending": {"date_from": "2020-01-01", "date_to": "2020-01-31"},
    "compare_spending": {"month": "2020-01", "baseline_month": "2019-12"},
    "goal_pace": {"goal_id": 0},
    "recurring_charges": {"lookback_days": 90, "min_count": 2},
    "confirm_recurring_status": {"merchant": "Netflix", "status": "active"},
    "submit_tip_feedback": {"tip_id": 1, "verdict": "accepted", "reason_text": "good tip"},
}

ALL_23_TOOLS = ZERO_ARG_TOOLS + list(KWARG_TOOLS)


def test_all_23_tools_enumerated_match_the_tools_schema_list():
    """Sanity check on this file's own fixtures, and a cheap pin that TOOLS
    (the LiteLLM-facing schema list) and _run_tool's dispatch table stay in
    sync at 23 entries with matching names — exactly the duplication epic
    deliverable #8 plans to collapse into one registry."""
    assert len(ALL_23_TOOLS) == 23
    assert set(ALL_23_TOOLS) == {t["name"] for t in app_module.TOOLS}


@pytest.mark.parametrize("tool_name", ZERO_ARG_TOOLS)
def test_zero_arg_tool_dispatches_with_no_args_and_ignores_inputs(monkeypatch, tool_name):
    mock = MagicMock(return_value=f"mocked-{tool_name}")
    monkeypatch.setattr(app_module, tool_name, mock)

    result = app_module._run_tool(tool_name, {"unexpected": "should be ignored"})

    mock.assert_called_once_with()
    assert result == f"mocked-{tool_name}"


@pytest.mark.parametrize("tool_name,inputs", KWARG_TOOLS.items())
def test_kwarg_tool_dispatches_with_forwarded_inputs(monkeypatch, tool_name, inputs):
    mock = MagicMock(return_value=f"mocked-{tool_name}")
    monkeypatch.setattr(app_module, tool_name, mock)

    result = app_module._run_tool(tool_name, inputs)

    mock.assert_called_once_with(**inputs)
    assert result == f"mocked-{tool_name}"


def test_unknown_tool_returns_error_string_without_raising():
    result = app_module._run_tool("not_a_real_tool", {})
    assert result == "Unknown tool: not_a_real_tool"


def test_tool_exception_is_caught_and_formatted_not_raised(monkeypatch):
    def _boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(app_module, "get_memory", _boom)

    result = app_module._run_tool("get_memory", {})

    assert result == "Tool error (get_memory): kaboom"


# ── real end-to-end smoke test ──────────────────────────────────────────────

def _seed_for_smoke_test(storage):
    from datetime import datetime, timedelta, timezone

    storage.save_session({
        "session_id": "sess-1",
        "accounts": [{
            "uid": "acc-1", "product": "Checking", "currency": "DKK",
            "account_id": {"iban": "DK1234567890"},
        }],
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    storage.store_balance("acc-1", {
        "balances": [{"balance_type": "closingBooked", "balance_amount": {"amount": "100.00", "currency": "DKK"}}],
    })

    import json
    import sqlite3
    import time
    import jyske_mcp.storage as storage_module
    conn = sqlite3.connect(str(storage_module._CACHE_DB))
    for i, (day, amount) in enumerate([("2020-01-05", 50.0), ("2020-01-15", 75.0)]):
        tid = f"smoke-tx-{i}"
        # raw_data must be populated -- get_transactions (the MCP tool, via
        # Storage.get_transactions_cached) json.loads()s it directly, same
        # as every real store_transaction()-written row would have.
        raw_data = json.dumps({
            "transaction_id": tid,
            "booking_date": day,
            "transaction_amount": {"amount": str(amount), "currency": "DKK"},
            "credit_debit_indicator": "DBIT",
            "creditor_name": "Some Shop",
        })
        conn.execute(
            "INSERT INTO transactions "
            "(account_uid, transaction_id, date, amount, currency, description, "
            " category_top, category_mid, created_at, direction, raw_data) "
            "VALUES ('acc-1', ?, ?, ?, 'DKK', 'Some Shop', 'Food & Dining', 'Restaurants', ?, 'DBIT', ?)",
            (tid, day, amount, time.time(), raw_data),
        )
    conn.commit()
    conn.close()

    storage.set_goal(agent_id="finance", name="Smoke goal", target_amount=1000.0,
                      purpose="test", deadline="2030-01-01")


@pytest.mark.parametrize("tool_name", ZERO_ARG_TOOLS)
def test_zero_arg_tool_executes_without_error_against_real_storage(full_schema_storage, tool_name):
    _seed_for_smoke_test(full_schema_storage)

    result = app_module._run_tool(tool_name, {})

    assert not str(result).startswith("Tool error ("), f"{tool_name} raised: {result}"


@pytest.mark.parametrize("tool_name,inputs", KWARG_TOOLS.items())
def test_kwarg_tool_executes_without_error_against_real_storage(full_schema_storage, tool_name, inputs):
    _seed_for_smoke_test(full_schema_storage)

    result = app_module._run_tool(tool_name, inputs)

    assert not str(result).startswith("Tool error ("), f"{tool_name} raised: {result}"
