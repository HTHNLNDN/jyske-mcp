"""
Characterization tests for jyske_mcp/slices/finance/registry.py's
TOOL_REGISTRY.dispatch — the chat tool-call dispatcher that maps an
LLM-issued tool name + JSON args to one of the 23 tool functions. No
litellm/LLM call happens anywhere in this file (dispatch itself never
calls an LLM — the 23 functions it dispatches to only ever touch local
SQLite via Storage, per the "MCP tools never call Enable Banking" rule),
so there's nothing to mock there; the zero-cost constraint is satisfied
simply by never invoking chat()/chat_completion.

Two layers of coverage, deliberately kept separate:

  1. Dispatch-table pinning (mock-based, no DB): each of the 23 tool names
     is proven to route to the correspondingly-named function with the
     input dict forwarded exactly as-is (or, for the 9 zero-arg tools,
     proven that the function is called with NO arguments and any input
     keys are silently ignored — current behavior, pinned since epic
     deliverable #8 collapsed the old TOOLS/LITELLM_TOOLS/run_tool
     three-way split into a single ToolRegistry and this behavior had to
     survive that collapse unchanged).

  2. A real end-to-end smoke test (full_schema_storage, no mocks) proving
     all 23 really execute without dispatch's try/except's "Tool error
     (...)" fallback firing, against a small seeded dataset covering every
     tool's data dependency.
"""
import dataclasses
from unittest.mock import create_autospec

import pytest

import jyske_mcp.slices.finance.registry as registry_module
from jyske_mcp.slices.finance.registry import TOOL_REGISTRY, ToolSpec

# Tools whose dispatch calls fn() with no arguments, ignoring whatever
# `inputs` dict was passed (see jyske_mcp/slices/finance/registry.py's
# ToolRegistry.dispatch — driven off the handler's own empty signature).
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

# Tools whose dispatch calls fn(**inputs) — the exact kwargs each needs,
# matching jyske_mcp/slices/finance/tools.py's real signatures.
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


def _patch_handler_autospec(monkeypatch, tool_name, return_value):
    """Swap the ToolSpec registered for `tool_name` so its handler is an
    autospec'd mock matching the real handler's signature (so
    inspect.signature(handler).parameters — what ToolRegistry.dispatch
    switches on — still reports the real zero-arg/kwarg shape, not a bare
    MagicMock's generic (*args, **kwargs)), leaving name/description/
    input_schema untouched. ToolSpec is a frozen dataclass (the handler
    callable is fixed at construction, by design — see registry.py), so
    tests replace the registry's internal name->spec entry rather than
    mutating a spec in place; monkeypatch restores the original entry
    after the test."""
    original = TOOL_REGISTRY._by_name[tool_name]
    mock = create_autospec(original.handler, return_value=return_value)
    monkeypatch.setitem(
        TOOL_REGISTRY._by_name, tool_name, dataclasses.replace(original, handler=mock)
    )
    return mock


def _patch_handler(monkeypatch, tool_name, handler):
    """Swap the ToolSpec registered for `tool_name` so its handler is
    `handler` verbatim (for a real function replacement, e.g. one that
    raises — no autospec needed since its own signature is already
    correct)."""
    original = TOOL_REGISTRY._by_name[tool_name]
    monkeypatch.setitem(
        TOOL_REGISTRY._by_name, tool_name, dataclasses.replace(original, handler=handler)
    )


def test_all_23_tools_enumerated_match_the_registry():
    """Sanity check on this file's own fixtures, and a cheap pin that
    TOOL_REGISTRY stays at 23 entries with matching names."""
    assert len(ALL_23_TOOLS) == 23
    assert set(ALL_23_TOOLS) == {s.name for s in TOOL_REGISTRY._specs}


@pytest.mark.parametrize("tool_name", ZERO_ARG_TOOLS)
def test_zero_arg_tool_dispatches_with_no_args_and_ignores_inputs(monkeypatch, tool_name):
    mock = _patch_handler_autospec(monkeypatch, tool_name, f"mocked-{tool_name}")

    result = TOOL_REGISTRY.dispatch(tool_name, {"unexpected": "should be ignored"})

    mock.assert_called_once_with()
    assert result == f"mocked-{tool_name}"


@pytest.mark.parametrize("tool_name,inputs", KWARG_TOOLS.items())
def test_kwarg_tool_dispatches_with_forwarded_inputs(monkeypatch, tool_name, inputs):
    mock = _patch_handler_autospec(monkeypatch, tool_name, f"mocked-{tool_name}")

    result = TOOL_REGISTRY.dispatch(tool_name, inputs)

    mock.assert_called_once_with(**inputs)
    assert result == f"mocked-{tool_name}"


def test_unknown_tool_returns_error_string_without_raising():
    result = TOOL_REGISTRY.dispatch("not_a_real_tool", {})
    assert result == "Unknown tool: not_a_real_tool"


def test_tool_exception_is_caught_and_formatted_not_raised(monkeypatch):
    def _boom():
        raise RuntimeError("kaboom")

    _patch_handler(monkeypatch, "get_memory", _boom)

    result = TOOL_REGISTRY.dispatch("get_memory", {})

    assert result == "Tool error (get_memory): kaboom"


# ── epic deliverable #8 acceptance: schema generation from one source ──────

def test_litellm_schema_is_generated_from_the_single_registry():
    """TOOL_REGISTRY.litellm_schemas() must be derived from the same
    ToolSpec list as anthropic_schemas() — one entry per spec, matching
    names, no separate hand-maintained LITELLM_TOOLS/dispatch-dict
    duplicate left anywhere in registry.py."""
    anthropic = TOOL_REGISTRY.anthropic_schemas()
    litellm = TOOL_REGISTRY.litellm_schemas()

    assert len(anthropic) == 23
    assert len(litellm) == 23

    anthropic_names = [t["name"] for t in anthropic]
    litellm_names = [t["function"]["name"] for t in litellm]

    # Same order, same names — both derived from the same ordered spec list.
    assert anthropic_names == litellm_names == [s.name for s in TOOL_REGISTRY._specs]
    assert set(anthropic_names) == set(ALL_23_TOOLS)

    for a, l in zip(anthropic, litellm):
        assert l["type"] == "function"
        assert l["function"]["description"] == a["description"]
        assert l["function"]["parameters"] == a["input_schema"]

    # No hand-maintained TOOLS/LITELLM_TOOLS/run_tool duplicate remains —
    # only the registry module + its ToolRegistry/ToolSpec classes.
    assert not hasattr(registry_module, "TOOLS")
    assert not hasattr(registry_module, "LITELLM_TOOLS")
    assert not hasattr(registry_module, "run_tool")


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
    import jyske_mcp.kernel.storage as storage_module
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

    result = TOOL_REGISTRY.dispatch(tool_name, {})

    assert not str(result).startswith("Tool error ("), f"{tool_name} raised: {result}"


@pytest.mark.parametrize("tool_name,inputs", KWARG_TOOLS.items())
def test_kwarg_tool_executes_without_error_against_real_storage(full_schema_storage, tool_name, inputs):
    _seed_for_smoke_test(full_schema_storage)

    result = TOOL_REGISTRY.dispatch(tool_name, inputs)

    assert not str(result).startswith("Tool error ("), f"{tool_name} raised: {result}"
